#!/usr/bin/env python

"""
.. module:: sen2three.L3_Tables
   :platform: Unix, Mac-OSX, Windows
   :synopsis: Level-3 IO module

.. moduleauthor:: umwilm


"""

import fnmatch
import subprocess
import threading
import os
import glob
import glymur
from PIL import Image
from scipy.ndimage.interpolation import zoom

from numpy import *
from tables import *
from lxml import etree, objectify
from tables.description import *
from distutils.dir_util import mkpath
from distutils.file_util import copy_file
from L3_XmlParser import L3_XmlParser
from L3_Borg import Borg
from L3_Library import showImage

try:
    from osgeo import gdal,osr
    from osgeo.gdalconst import *
    gdal.TermProgress = gdal.TermProgress_nocb
except ImportError:
    import gdal,osr
    from gdalconst import *
# SIITBX-47: to suppress user warning due to the fact that 
# http://trac.osgeo.org/gdal/ticket/5480 is not implemented
# in the current openJPEG driver for windows used by ANACONDA:
gdal.PushErrorHandler('CPLQuietErrorHandler')

threadLock = threading.Lock()


class Particle(IsDescription):
    bandName = StringCol(8)
    projectionRef = StringCol(512)
    geoTransformation = Int32Col(shape=6)
    rasterXSize = UInt16Col()
    rasterYSize = UInt16Col()
    rasterCount = UInt8Col()


class gdalThreadRead(threading.Thread):
    def __init__(self, bandIndex, tables, filename):
        threading.Thread.__init__(self)
        self._bandIndex = bandIndex
        self._tables = tables
        self._config = tables.config
        self._filename = filename

    def run(self):
        command = 'gdal_translate '
        filename = self._filename
        tmpfile = self._tables._L3_bandDir + '/.tmpfile_' + '%02d.tif' % self._bandIndex
        if os.name == 'posix':
            DEV0 = ' > /dev/null'
        else:
            DEV0 = ' > NUL'
        
        callstr = command + filename + ' ' + tmpfile + DEV0
        if(subprocess.call(callstr, shell=True) != 0):
            self._config.logger.fatal('shell execution error using gdal_translate')
            self._config.exitError()
            return False
        
        with threadLock:
            if(os.path.isfile(tmpfile)):
                os.remove(tmpfile)
        return True


class gdalThreadWrite(threading.Thread):
    def __init__(self, bandIndex, tables, filename):
        threading.Thread.__init__(self)
        self._bandIndex = bandIndex
        self._config = tables.config
        self._tables = tables
        self._filename = filename

    def run(self):
        if os.name == 'posix':
            DEV0 = ' > /dev/null'
        else:
            DEV0 = ' > NUL'
        tables = self._tables
        tmpfile = tables.L3_bandDir + '/.tmpfile_' + '%02d.tif' % self._bandIndex
        option2 = ' UInt16 '    
            
        if os.name == 'posix':
            callstr = 'gdal_translate -of JPEG2000 -ot' + option2 + tmpfile + ' ' + self._filename + DEV0
        else: # windows
            callstr = 'geojasper -f ' + tmpfile + ' -F ' + self._filename + ' -T jp2 > NUL'
            
        if(subprocess.call(callstr, shell=True) != 0):
            self._config.logger.fatal('shell execution error using gdal_translate')
            self._config.exitError()
            return False
            
        if(os.path.isfile(tmpfile)):
            os.remove(tmpfile) 

        self._filename = os.path.basename(self._filename.strip('.jp2'))
        with threadLock:
            '''
            imageId = L3_UserProduct.IMAGE_IDType()
            imageId.set_valueOf_(self._filename)
            tables.granuleType.add_IMAGE_ID(imageId)
            '''
        bandName = tables.getBandNameFromIndex(self._bandIndex)
        self._config.logger.debug('Band: ' + bandName + ' converted')    
        return
        

class L3_Tables(Borg):
    ''' A support class, managing the conversion of the JPEG-2000 based input data
        to an internal format based on HDF5 (pyTables). Provide a high performance
        access to the image data for all bands of all tiles to be processed.
        
        :param config: the config object for the current tile.
        :type config: a reference to the L3_Config object.

    '''

    def __init__(self, config):
        self.config = config

        AUX_DATA = '/AUX_DATA'
        IMG_DATA = '/IMG_DATA'
        QI_DATA = '/QI_DATA'
        GRANULE = '/GRANULE'

        if os.name == 'posix':
            self._DEV0 = ' > /dev/null'
        else:
            self._DEV0 = ' > NUL'

        # Resolution:
        self._resolution = int(config.resolution)
        if(self._resolution == 10):
            self._bandIndex = [1,2,3,7]
            self._nBands = 4
            bandDir = '/R10m'
        elif(self._resolution == 20):
            self._bandIndex = [1,2,3,4,5,6,8,11,12]
            self._nBands = 9
            bandDir = '/R20m'
        elif(self._resolution == 60):
            self._bandIndex = [0,1,2,3,4,5,6,8,9,11,12]
            self._nBands = 11
            bandDir = '/R60m'

        BANDS = bandDir
        self._rows = None
        self._cols = None
        self._granuleType = None
        self._threads = []
        # generate new Tile ID:
        # check whether a tile with same ORBIT ID already exists, if yes use this folder.
        # else create a new tile folder with corresponding orbit ID
        L2A_TILE_ID = config.product.L2A_TILE_ID
        L3_TILE_MSK = 'S2A_*_TL_*'
        ORBIT_ID = L2A_TILE_ID[-13:-7]
        L3_TILE_ID = ''
        L3_TARGET_ID = config.product.L3_TARGET_ID
        tiles = config.targetDir + '/' + L3_TARGET_ID + GRANULE
        files = sorted(os.listdir(tiles))
        for L3_TILE_ID in files:       
            if fnmatch.fnmatch(L3_TILE_ID, L3_TILE_MSK) == True:
                break
        if ORBIT_ID in L3_TILE_ID:
        # target exists, will be used:
            config.product.reinitL2A_Tile()
            config.product.reinitL3_Tile(L3_TILE_ID)
        else:
            config.product.createL3_Tile(L2A_TILE_ID)
        L2A_UP_ID = config.product.L2A_UP_ID
        L3_TILE_ID = config.product.L3_TILE_ID
        L2A_TILE_ID_SHORT = '/' + L2A_TILE_ID[:55]
        L3_TILE_ID_SHORT = '/' + config.product.L3_TILE_ID[:55]            
        L2A_TILE_ID = config.sourceDir + '/' + L2A_UP_ID + GRANULE + '/' + L2A_TILE_ID
        L3_TILE_ID = config.targetDir + '/' + L3_TARGET_ID + GRANULE + '/' + L3_TILE_ID
        self._L2A_ImgDataDir = L2A_TILE_ID + IMG_DATA
        self._L3_ImgDataDir = L3_TILE_ID + IMG_DATA
        self._L2A_bandDir = self._L2A_ImgDataDir + BANDS
        self._L3_bandDir = self._L3_ImgDataDir + BANDS

        if(os.path.exists(self._L3_bandDir) == False):
            mkpath(self._L3_bandDir)

        self._L2A_QualityDataDir = L2A_TILE_ID + QI_DATA
        self._L3_QualityDataDir = L3_TILE_ID + QI_DATA
        self._L3_AuxDataDir = L3_TILE_ID + AUX_DATA

        if(os.path.exists(self._L3_AuxDataDir) == False):
            mkpath(self._L3_AuxDataDir)
            # copy configuration to AUX dir:
            dummy, basename = os.path.split(config.product.L3_TILE_MTD_XML)
            fnAux = basename.replace('_MTD_', '_GIP_')
            target = self._L3_AuxDataDir + '/' + fnAux
            copy_file(config.configFn, target)

        if(os.path.exists(self._L3_QualityDataDir) == False):
            mkpath(self._L3_QualityDataDir)

        #
        # the File structure:
        #-------------------------------------------------------
        pre = L2A_TILE_ID_SHORT[:9]
        post = L2A_TILE_ID_SHORT[13:]
        self._L2A_Tile_BND_File = self._L2A_bandDir + L2A_TILE_ID_SHORT + '_BXX_' + str(self._resolution) + 'm.jp2'
        self._L2A_Tile_AOT_File = self._L2A_bandDir        + pre + '_AOT' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L2A_Tile_WVP_File = self._L2A_bandDir        + pre + '_WVP' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L2A_Tile_CLD_File = self._L2A_QualityDataDir + pre + '_CLD' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L2A_Tile_SNW_File = self._L2A_QualityDataDir + pre + '_SNW' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L2A_Tile_PVI_File = self._L2A_QualityDataDir + pre + '_PVI' + post + '.jp2'
        self._L2A_Tile_SCL_File = self._L2A_ImgDataDir     + pre + '_SCL' + post + '_' + str(self._resolution) + 'm.jp2'        

        pre = L3_TILE_ID_SHORT[:9]
        post = L3_TILE_ID_SHORT[13:]
        self._L3_Tile_BND_File = self._L3_bandDir + L3_TILE_ID_SHORT + '_BXX_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_AOT_File = self._L3_bandDir        + pre + '_AOT' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_WVP_File = self._L3_bandDir        + pre + '_WVP' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_CLD_File = self._L3_QualityDataDir + pre + '_CLD' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_SNW_File = self._L3_QualityDataDir + pre + '_SNW' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_MSC_File = self._L3_QualityDataDir + pre + '_MSC' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_PVI_File = self._L3_QualityDataDir + pre + '_PVI' + post + '_' + str(self.config.nrTilesProcessed) + '.jp2'
        self._L3_Tile_PLT_File = self._L3_QualityDataDir + pre + '_PLT' + post + '_' + str(self.config.nrTilesProcessed) + '.png'
        self._L3_Tile_SCL_File = self._L3_QualityDataDir + pre + '_SCL' + post + '_' + str(self._resolution) + 'm.jp2'

        self._imageDatabase = self._L3_bandDir + '/.database.h5'
        self._TmpFile = self._L3_bandDir + '/.tmpfile_'

        # Geodata from image metadata:
        self._cornerCoordinates = None
        self._geoTransformation = None
        self._geoExtent = None
        self._projectionRef = None

        # Product Levels:
        self._productLevel = ['L1C','L2A','L3']
        
        # the mapping of the product levels
        self._L1C = 0
        self._L2A = 1
        self._L03 = 2

        # Band Names:
        self._bandNames = ['B01','B02','B03','B04','B05','B06','B07','B08','B8A',\
                        'B09','B10','B11','B12','DEM','SCL','SNW','CLD','AOT',\
                        'WVP','VIS','SCM','PRV','ILU','SLP','ASP','HAZ','SDW',\
                        'DDV','HCW','ELE', 'MSC']
        
        # the mapping of the bands
        self._B01 = 0
        self._B02 = 1
        self._B03 = 2
        self._B04 = 3
        self._B05 = 4
        self._B06 = 5
        self._B07 = 6
        self._B08 = 7
        self._B8A = 8
        self._B09 = 9
        self._B10 = 10
        self._B11 = 11
        self._B12 = 12
        self._DEM = 13
        self._SCL = 14
        self._SNW = 15
        self._CLD = 16
        self._AOT = 17
        self._WVP = 18
        self._VIS = 19
        self._SCM = 20
        self._PRV = 21
        self._ILU = 22
        self._SLP = 23
        self._ASP = 24
        self._HAZ = 25
        self._SDW = 26
        self._DDV = 27
        self._HCW = 28
        self._ELE = 29
        self._MSC = 30

        config.logger.debug('Module L3_Tables initialized with resolution %d' % self._resolution)

        return

    def get_l_3_tile_plt_file(self):
        return self._L3_Tile_PLT_File


    def set_l_3_tile_plt_file(self, value):
        self._L3_Tile_PLT_File = value


    def del_l_3_tile_plt_file(self):
        del self._L3_Tile_PLT_File


    def get_msc(self):
        return self._MSC


    def set_msc(self, value):
        self._MSC = value


    def del_msc(self):
        del self._MSC


    def get_aot(self):
        return self._AOT


    def set_aot(self, value):
        self._AOT = value


    def del_aot(self):
        del self._AOT


    def get_corner_coordinates(self):
        return self._cornerCoordinates


    def get_geo_extent(self):
        return self._geoExtent


    def get_projection(self):
        return self._projection


    def set_corner_coordinates(self, value):
        self._cornerCoordinates = value


    def set_geo_extent(self, value):
        self._geoExtent = value


    def set_projection(self, value):
        self._projection = value


    def del_corner_coordinates(self):
        del self._cornerCoordinates


    def del_geo_extent(self):
        del self._geoExtent


    def del_projection(self):
        del self._projection


    def getBandNameFromIndex(self, bandIndex):
        return self._bandNames[bandIndex]


    def get_band_bandIndex(self):
        return self._bandIndex


    def get_n_bands(self):
        return self._nBands


    def get_db_name(self):
        return self._dbName


    def set_band_bandIndex(self, value):
        self._bandIndex = value


    def set_n_bands(self, value):
        self._nBands = value


    def set_db_name(self, value):
        self._dbName = value


    def del_band_bandIndex(self):
        del self._bandIndex


    def del_n_bands(self):
        del self._nBands


    def del_db_name(self):
        del self._dbName


        # end mapping of bands

    def __exit__(self):
        sys.exit(-1)


    def __del__(self):
        self.config.logger.debug('Module L3_Tables deleted')


    def get_config(self):
        return self._config


    def set_config(self, value):
        self._config = value


    def del_config(self):
        del self._config


    def get_b01(self):
        return self._B01


    def get_b02(self):
        return self._B02


    def get_b03(self):
        return self._B03


    def get_b04(self):
        return self._B04


    def get_b05(self):
        return self._B05


    def get_b06(self):
        return self._B06


    def get_b07(self):
        return self._B07


    def get_b08(self):
        return self._B08


    def get_b8a(self):
        return self._B8A


    def get_b09(self):
        return self._B09


    def get_b10(self):
        return self._B10


    def get_b11(self):
        return self._B11


    def get_b12(self):
        return self._B12


    def get_scl(self):
        return self._SCL


    def get_qsn(self):
        return self._SNW


    def get_qcl(self):
        return self._CLD


    def get_prv(self):
        return self._PRV


    def set_b01(self, value):
        self._B01 = value


    def set_b02(self, value):
        self._B02 = value


    def set_b03(self, value):
        self._B03 = value


    def set_b04(self, value):
        self._B04 = value


    def set_b05(self, value):
        self._B05 = value


    def set_b06(self, value):
        self._B06 = value


    def set_b07(self, value):
        self._B07 = value


    def set_b08(self, value):
        self._B08 = value


    def set_b8a(self, value):
        self._B8A = value


    def set_b09(self, value):
        self._B09 = value


    def set_b10(self, value):
        self._B10 = value


    def set_b11(self, value):
        self._B11 = value


    def set_b12(self, value):
        self._B12 = value


    def set_dem(self, value):
        self._DEM = value


    def set_scl(self, value):
        self._SCL = value


    def set_qsn(self, value):
        self._SNW = value


    def set_qcl(self, value):
        self._CLD = value


    def set_prv(self, value):
        self._PRV = value


    def del_b01(self):
        del self._B01


    def del_b02(self):
        del self._B02


    def del_b03(self):
        del self._B03


    def del_b04(self):
        del self._B04


    def del_b05(self):
        del self._B05


    def del_b06(self):
        del self._B06


    def del_b07(self):
        del self._B07


    def del_b08(self):
        del self._B08


    def del_b8a(self):
        del self._B8A


    def del_b09(self):
        del self._B09


    def del_b10(self):
        del self._B10


    def del_b11(self):
        del self._B11


    def del_b12(self):
        del self._B12


    def del_scl(self):
        del self._SCL


    def del_qsn(self):
        del self._SNW


    def del_qcl(self):
        del self._CLD


    def del_prv(self):
        del self._PRV

    AOT = property(get_aot, set_aot, del_aot)
    B01 = property(get_b01, set_b01, del_b01)
    B02 = property(get_b02, set_b02, del_b02)
    B03 = property(get_b03, set_b03, del_b03)
    B04 = property(get_b04, set_b04, del_b04)
    B05 = property(get_b05, set_b05, del_b05)
    B06 = property(get_b06, set_b06, del_b06)
    B07 = property(get_b07, set_b07, del_b07)
    B08 = property(get_b08, set_b08, del_b08)
    B8A = property(get_b8a, set_b8a, del_b8a)
    B09 = property(get_b09, set_b09, del_b09)
    B10 = property(get_b10, set_b10, del_b10)
    B11 = property(get_b11, set_b11, del_b11)
    B12 = property(get_b12, set_b12, del_b12)
    SCL = property(get_scl, set_scl, del_scl)
    SNW = property(get_qsn, set_qsn, del_qsn)
    CLD = property(get_qcl, set_qcl, del_qcl)
    MSC = property(get_msc, set_msc, del_msc)
    PRV = property(get_prv, set_prv, del_prv)
    L3_Tile_PLT_File = property(get_l_3_tile_plt_file, set_l_3_tile_plt_file, del_l_3_tile_plt_file)

    config = property(get_config, set_config, del_config)
    bandIndex = property(get_band_bandIndex, set_band_bandIndex, del_band_bandIndex)
    nBands = property(get_n_bands, set_n_bands, del_n_bands)
    dbName = property(get_db_name, set_db_name, del_db_name)
    cornerCoordinates = property(get_corner_coordinates, set_corner_coordinates, del_corner_coordinates)
    geoExtent = property(get_geo_extent, set_geo_extent, del_geo_extent)
    projection = property(get_projection, set_projection, del_projection)

    def reprojectCoords(self,coords,src_srs,tgt_srs):
        ''' Reproject a list of x,y coordinates.
        
            :param coords: List of [[x,y],...[x,y]] coordinates
            :type coords: C{tuple/list}
            :param src_srs: OSR SpatialReference object
            :type src_srs: C{osr.SpatialReference}
            :param tgt_srs: OSR SpatialReference object
            :type tgt_srs: C{osr.SpatialReference}
            :return: List of transformed [[x,y],...[x,y]] coordinates
            :rtype: C{tuple/list}
            
        '''
        trans_coords=[]
        transform = osr.CoordinateTransformation( src_srs, tgt_srs)
        for x,y in coords:
            x,y,z = transform.TransformPoint(x,y)
            trans_coords.append([x,y])
        return trans_coords

    def getExtent(self, gt,cols,rows):
        ''' Return list of corner coordinates from an osgeo geotransform.
        
            :param gt: geotransform
            :type gt: C{tuple/list}
            :param cols: number of columns in the dataset
            :type cols: C{int}
            :param rows: number of rows in the dataset
            :type rows: C{int}
            :return: coordinates of each corner
            :rtype: C{[float,...,float]}

        '''
        ext=[]
        xarr=[0,cols]
        yarr=[0,rows]

        for px in xarr:
            for py in yarr:
                x=gt[0]+(px*gt[1])+(py*gt[2])
                y=gt[3]+(px*gt[4])+(py*gt[5])
                ext.append([x,y])
            yarr.reverse()
        return ext
    
    def init(self):
        ''' Checks the existence of a L3 target database for the processed tile.
            If the database exists, the given tile will be imported. If the database does not exist
            it will be created and the current tile will become the base for the subsequent processing.
        '''
        self._config.logger.info('Checking existence of L3 target database')
        try:
            h5file = open_file(self._imageDatabase)
            h5file.close()
            self.importBandList('L2A')
        except:
            self.initDatabase()
            self.importBandList('L3')
            return
        #if(self.config.resolution == 60):            
        self.createPreviewImage('L3')
        self.createPreviewImage('L2A')
        return

    def exportTile(self, L3_TILE_ID):
        ''' Prepare the export of a synthesized tile.
        
           :param identifier: the tile ID.
           :type identifier: str
           
        '''

        AUX_DATA = '/AUX_DATA'
        IMG_DATA = '/IMG_DATA'
        QI_DATA = '/QI_DATA'
        GRANULE = '/GRANULE'
        # Resolution:
        self._resolution = int(self.config.resolution)
        if(self._resolution == 10):
            bandDir = '/R10m'
        elif(self._resolution == 20):
            bandDir = '/R20m'
        elif(self._resolution == 60):
            bandDir = '/R60m'
        BANDS = bandDir
        L3_TARGET_ID = self.config.product.L3_TARGET_ID
        L3_TILE_ID_SHORT = '/' + L3_TILE_ID[:55]            
        L3_TILE_ID = self.config.targetDir + '/' + L3_TARGET_ID + GRANULE + '/' + L3_TILE_ID
        self._L3_ImgDataDir = L3_TILE_ID + IMG_DATA
        self._L3_bandDir = self._L3_ImgDataDir + BANDS
        self._L3_QualityDataDir = L3_TILE_ID + QI_DATA
        self._L3_AuxDataDir = L3_TILE_ID + AUX_DATA
        #
        # the File structure:
        #-------------------------------------------------------
        pre = L3_TILE_ID_SHORT[:9]
        post = L3_TILE_ID_SHORT[13:]
        self._L3_Tile_BND_File = self._L3_bandDir + L3_TILE_ID_SHORT + '_BXX_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_AOT_File = self._L3_bandDir        + pre + '_AOT' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_WVP_File = self._L3_bandDir        + pre + '_WVP' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_CLD_File = self._L3_QualityDataDir + pre + '_CLD' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_SNW_File = self._L3_QualityDataDir + pre + '_SNW' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_MSC_File = self._L3_QualityDataDir + pre + '_MSC' + post + '_' + str(self._resolution) + 'm.jp2'
        self._L3_Tile_PVI_File = self._L3_QualityDataDir + pre + '_PVI' + post + '_' + str(self.config.nrTilesProcessed) + '.jp2'
        self._L3_Tile_PLT_File = self._L3_QualityDataDir + pre + '_PLT' + post + '_' + str(self.config.nrTilesProcessed) + '.png'
        self._L3_Tile_SCL_File = self._L3_QualityDataDir + pre + '_SCL' + post + '_' + str(self._resolution) + 'm.jp2'

        self._imageDatabase = self._L3_bandDir + '/.database.h5'
        self.config.logger.debug('Module L3_Tables reinitialized with resolution %d' % self._resolution)
        self.exportBandList('L3')
        return
    
    def initDatabase(self):
        ''' Initialize H5 target database for usage.
        '''
        try:
            h5file = open_file(self._imageDatabase, mode='a', title =  str(self._resolution) + 'm bands')
            # remove all existing L2A tables as they will be replaced by the new data set
            h5file.create_group('/', 'L1C', 'bands L1C')
            h5file.create_group('/', 'L2A', 'bands L2A')
            h5file.create_group('/', 'L3', 'bands L3')
            result = True
        except:
            self.config.logger.fatal('error in initialization of database: %s:' % self._imageDatabase)
            self.config.exitError()
            result = False
        h5file.close()
        return result
    
    def importBandList(self, productLevel):
        ''' Import all bands of current tile.
            
            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :rtype: none.
            
        '''        
        self.config.timestamp('L3_Tables: start import')
        self._productLevel = productLevel        
        bandDir = self._L2A_bandDir
        os.chdir(bandDir)
        dirs = sorted(os.listdir(bandDir))
        bands = self._bandIndex
        for i in bands:
            for filename in dirs:  
                bandName = self.getBandNameFromIndex(i)   
                filemask = '*_L2A_*_B%2s_??m.jp2' % bandName[1:3]
                if fnmatch.fnmatch(filename, filemask) == False:
                    continue
                self.importBand(i, filename)  
        if os.path.isfile(self._L2A_Tile_AOT_File):
            self.importBand(self._AOT, self._L2A_Tile_AOT_File)
        if os.path.isfile(self._L2A_Tile_CLD_File):
            self.importBand(self._CLD, self._L2A_Tile_CLD_File)
        if os.path.isfile(self._L2A_Tile_SNW_File):
            self.importBand(self._SNW, self._L2A_Tile_SNW_File)
        if os.path.isfile(self._L2A_Tile_SCL_File):
            self.importBand(self._SCL, self._L2A_Tile_SCL_File)
        return
    

    def getBand(self, productLevel, bandIndex, dataType=uint16):
        ''' Get a single band from database.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str            
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :param dataType: data type of band.
            :type dataType: default: unsigned int 16
            :return: the pixel data.
            :rtype: a 2 dimensional numpy array (row x column) of type unsigned int 16 
            
        '''
        self.verifyProductId(productLevel)
        bandName = self.getBandNameFromIndex(bandIndex)
        try:
            h5file = open_file(self._imageDatabase)
            node = h5file.getNode('/' + productLevel, bandName)
            if (node.dtype != dataType):
                self.config.logger.fatal('Wrong data type, must be: ' + str(node.dtype))
                result = False
                self.config.exitError()
            else:
                result = node.read()
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s is missing', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False
        h5file.close()
        return result

    def importBand(self, bandIndex, filename):
        ''' convert JPEG-2000 input file to internal H5 file format.
        
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :param filename: file name of JPEG-2000 input image.
            :type filename: str
            :return: false if error occurred during import.
            :rtype: boolean
            
        ''' 
        # convert JPEG-2000 input file to H5 file format
        self.verifyProductId(self._productLevel)
        warnings.filterwarnings("ignore")
        kwargs = {"tilesize": (2048, 2048), "prog": "RPCL"}
        indataset = glymur.Jp2k(filename, **kwargs)  
        ncols = indataset.shape[1]
        indataArr = indataset[:]
        indataset = None
        # Create new arrays:
        database = self._imageDatabase
        nodeStr = self._productLevel
        bandName = self.getBandNameFromIndex(bandIndex)
        try:
            if self.testBand(self._productLevel, bandIndex) == True:
                self.delBand(self._productLevel, bandIndex)
            h5file = open_file(database, mode='a')
            if(h5file.__contains__('/' + nodeStr)) == False:
                self.config.logger.fatal('table initialization, wrong node %s:' % nodeStr)
                self.config.exitError()
                return False
                
            if self._productLevel == 'L1C':
                locator = h5file.root.L1C
            elif self._productLevel == 'L2A':
                locator = h5file.root.L2A
            elif self._productLevel == 'L3':
                locator = h5file.root.L3
                
            dtOut = self.mapDataType(indataArr.dtype)
            filters = Filters(complib="zlib", complevel=1)
            node = h5file.createEArray(locator, bandName, dtOut, (0,ncols), bandName, filters=filters)
            node.append(indataArr)
            self.config.timestamp('L3_Tables: Level ' + self._productLevel + ' band ' + bandName + ' imported')
            result = True
        except:
            indataArr = None
            self.config.logger.fatal('error in import of band %s in productLevel %s.' % (bandName, self._productLevel))
            self.config.exitError()
            result = False

        indataArr = None
        h5file.close()
        return result


    def exportBandList(self, productLevel):
        ''' Export all bands of current tile.
            converts all bands from hdf5 to JPEG-2000.
            
            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :return: false if error occurred during export.
            :rtype: boolean           

        '''     
        bandDir = self._L3_bandDir
        # converts all bands from hdf5 to JPEG 2000
        if(os.path.exists(bandDir) == False):
            self.config.logger.fatal('missing directory %s:' % bandDir)
            self.config.exitError()
            return False

        os.chdir(bandDir)
        self.config.timestamp(productLevel + '_Tables: start export')
        if(self._resolution == 10):
            bandIndex = [1,2,3,7,14,30]

        elif(self._resolution == 20):
            bandIndex = [1,2,3,4,5,6,8,11,12,14,30]

        elif(self._resolution == 60):
            bandIndex = [0,1,2,3,4,5,6,8,9,11,12,14,30]

        #prepare the xml export
        Granules = objectify.Element('Granules')
        Granules.attrib['granuleIdentifier'] = self.config.product.L3_TILE_ID
        Granules.attrib['datastripIdentifier'] = self.config.product.L3_DS_ID
        Granules.attrib['imageFormat'] = 'JPEG2000'
        gl = objectify.Element('Granule_List')
        gl.append(Granules)
        for index in bandIndex:
            bandName = self.getBandNameFromIndex(index)
            if index == 14:
                filename = self._L3_Tile_SCL_File
            elif index == 30:
                filename = self._L3_Tile_MSC_File
            else:
                filename = self._L3_Tile_BND_File.replace('BXX', bandName)
            band = self.getBand(productLevel, index)
            kwargs = {"tilesize": (2048, 2048), "prog": "RPCL"}
            glymur.Jp2k(filename, band.astype(uint16), **kwargs)            
            self.config.logger.info('Band ' + bandName + ' exported')
            self.config.timestamp('L3_Tables: band ' + bandName + ' exported')
            filename = os.path.basename(filename.strip('.jp2'))
            imageId = etree.Element('IMAGE_ID_2A')
            imageId.text = filename
            Granules.append(imageId)
        '''
        xp = L3_XmlParser(self.config, 'UP03')
        pi = xp.getTree('General_Info', 'L3_Product_Info')
        po = pi.L3_Product_Organisation
        po.append(gl)
        xp.export()
        
        # update on tile level
        xp = L3_XmlParser(self.config, 'T03')
        ti = xp.getTree()
        xmlParser.root.General_Info.TILE_ID.valueOf_ = self.config.product.L3_TILE_ID
        xmlParser.root.General_Info.DATASTRIP_ID.valueOf_ =self.config.product.L3_DS_ID

        # clean up and post processing actions:
            qiiL3 = xmlParser.root.Quality_Indicators_Info
            qiiL3.PVI_FILENAME = os.path.basename(self._L3_Tile_PVI_File)
            qiiL3 = xmlParser.root.Quality_Indicators_Info
            qiList = L3_Tile.A_L3_Pixel_Level_QI_LIST()
            qiiL3.L3_Pixel_Level_QI = qiList
        xmlParser.export()
        '''
        globdir = self.config.product.L3_TARGET_ID + '/GRANULE/' + self.config.product.L3_TILE_ID + '/*/*.jp2.aux.xml'
        for filename in glob.glob(globdir):
            os.remove(filename)
        globdir = self.config.product.L3_TARGET_ID + '/GRANULE/' + self.config.product.L3_TILE_ID + '/*/*/*.jp2.aux.xml'
        for filename in glob.glob(globdir):
            os.remove(filename)
        self.config.timestamp(productLevel + '_Tables: stop export')
        return True

    def scaleImgArray(self, arr):
        ''' Scale image array for preview. Helper function used by createPreviewImage().

            :param arr: the image array.
            :type arr: 2 dimensional numpy array (nrow x ncols).
            :return: false if image cannot be created, else true.            
            :rtype: boolean
            
        '''
        if(arr.ndim) != 2:
            self.config.logger.fatal('must be a 2 dimensional array')
            self.config.exitError()
            return False
        '''
        src_nrows = arr.shape[0]
        src_ncols = arr.shape[1]
        tgt_ncols = 343.0
        tgt_nrows = 343.0

        zoomX = float64(tgt_ncols)/float64(src_ncols)
        zoomY = float64(tgt_nrows)/float64(src_nrows)
        arr = zoom(arr, ([zoomX,zoomY]), order=0)        
        '''
        arrclip = arr.copy()
        min_ = 0.0
        max_ = 250
        scale = 255.0
        arr = clip(arrclip, min_, max_)
        #SIITBX-50: wrong scale was used: 
        scaledArr = uint8(arr*scale/max_)
        return scaledArr

    def setBand(self, productLevel, bandIndex, arr):
        ''' Set a single band from numpy array to H5 database.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :param arr: the pixel data.
            :type arr: a 2 dimensional numpy array (row x column) of type unsigned int 16.            
            :return: false if error occurred during setting of band.            
            :rtype: boolean
            
        '''  
        self.verifyProductId(productLevel)
        try:
            if self.testBand(productLevel, bandIndex) == True:
                self.delBand(productLevel, bandIndex)
            h5file = open_file(self._imageDatabase, mode='a')
            bandName = self.getBandNameFromIndex(bandIndex)
            dtIn = self.mapDataType(arr.dtype)
            filters = Filters(complib="zlib", complevel=1)
            # create new group and append node:
            if productLevel == 'L1C':
                locator = h5file.root.L1C
            elif productLevel == 'L2A':
                locator = h5file.root.L2A
            elif productLevel == 'L3':
                locator = h5file.root.L3

            node = h5file.createEArray(locator, bandName, dtIn, (0,arr.shape[1]), bandName, filters=filters)
            self.config.logger.debug('%s: Band %02d %s added to table', productLevel, bandIndex, self.getBandNameFromIndex(bandIndex))
            node.append(arr)
            result = True
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s cannot be set', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False
        h5file.close()
        return result

    def delBand(self, productLevel, bandIndex):
        ''' Delete a single band from H5 database.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :return: false if error occurred during deletion of band.            
            :rtype: boolean
            
        '''  
        self.verifyProductId(productLevel)
        try:
            h5file = open_file(self._imageDatabase, mode='a')
            bandName = self.getBandNameFromIndex(bandIndex)
            if(h5file.__contains__('/' + productLevel + '/' + bandName)):
                node = h5file.getNode('/' + productLevel, bandName)
                node.remove()
                self.config.logger.debug('%s: Band %02d %s removed from table', productLevel, bandIndex, self.getBandNameFromIndex(bandIndex))
            result = True
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s cannot be removed', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False
        h5file.close()
        return result

    def delBandList(self, productLevel):
        ''' Delete the complete list of bands from H5 database.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :return: false if error occurred during deletion of band.            
            :rtype: boolean
            
        ''' 
        try:
            h5file = open_file(self._imageDatabase, mode='a')
            if(h5file.__contains__('/' + productLevel)):
                node = h5file.getNode('/' + productLevel)
                del node
                self.config.logger.debug('%s: Bands removed from table', productLevel)
            result = True
        except NoSuchNodeError:
            self.config.logger.debug('%s: Bands cannot be removed', productLevel)
            result = False          
        h5file.close()
        return result            
        
    def delDatabase(self):
        ''' Delete the H5 database.

            :return: true if succeeds, false if database does not exist.            
            :rtype: boolean
            
        ''' 
        database = self._imageDatabase
        if os.path.isfile(database):
            os.remove(database)
            self.config.logger.debug('%s: removed', database)
            return True
        else:
            self.config.logger.debug('%s: does not exist', database)
            return False

    def createPreviewImage(self, productLevel):
        ''' Create an RGB preview image from bands 2-4.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :return: false if image cannot be created, else true.            
            :rtype: boolean
            
        '''
        self.config.logger.debug('Creating Preview Image')
        if productLevel == 'L2A':
            filename = self._L2A_Tile_PVI_File            
        else:
            filename = self._L3_Tile_PVI_File
            
        b = self.getBand(productLevel, self.B02)
        g = self.getBand(productLevel, self.B03)
        r = self.getBand(productLevel, self.B04)

        b = self.scaleImgArray(b)
        g = self.scaleImgArray(g)
        r = self.scaleImgArray(r)

        b = Image.fromarray(b)
        g = Image.fromarray(g)
        r = Image.fromarray(r)

        try:
            out = Image.merge('RGB', (r,g,b))
            a = array(out)
            kwargs = {"tilesize": (2048, 2048), "prog": "RPCL"}
            glymur.Jp2k(filename, a.astype(uint8), **kwargs)   
            self.config.logger.debug('Preview Image created')
            return True
        except:
            self.config.logger.fatal('Preview Image creation failed')
            self.config.exitError()
            return False

    def testDb(self):
        ''' Test consistency of database.
        
            :param: none.
            :return: false if database is inconsistent, else true.            
            :rtype: boolean
            
        '''
        result = False
        try:
            h5file = open_file(self._imageDatabase)
            h5file.getNode('/L3', 'B02')
            h5file.getNode('/L3', 'B03')
            h5file.getNode('/L3', 'B04')
            status = 'Database ' + self._imageDatabase + ' exists and can be used'
            result = True
        except:
            status = 'Database  ' + self._imageDatabase + ' will be removed due to corruption'
            self.removeDatabase()
            result = False
        h5file.close()
        self.config.logger.info(status)
        return result
    
    def verifyProductId(self, productLevel):
        ''' Verify the product ID.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :return: true if product level exists, else false.            
            :rtype: boolean
            
        '''
        if productLevel != 'L1C' and productLevel != 'L2A' and productLevel != 'L3':
            self.config.logger.fatal('Wrong product ID %s', productLevel)
            self.config.exitError()
        return True
    
    def testBand(self, productLevel, bandIndex):
        ''' Test if band exists in database.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :return: true if band exists, else false.            
            :rtype: boolean
            
        '''        
        self.verifyProductId(productLevel)
        bandName = self.getBandNameFromIndex(bandIndex)
        try:
            h5file = open_file(self._imageDatabase)
            h5file.getNode('/' + productLevel , bandName)
            self.config.logger.debug('%s: Band %s is present', productLevel, self.getBandNameFromIndex(bandIndex))
            result = True
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s is missing', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False
        h5file.close()
        return result

    def getBandSize(self, productLevel, bandIndex):
        ''' Get size of image array.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :return: image size (nrows x ncols)            
            :rtype: data tuple (unsigned int).
            
        '''
        self.verifyProductId(productLevel)
        bandName = self.getBandNameFromIndex(bandIndex)
        try:
            h5file = open_file(self._imageDatabase)
            node = h5file.getNode('/' + productLevel, bandName)
            arr = node.read()
            ncols = arr.shape[1]
            nrows = arr.shape[0]
            result = (nrows, ncols)
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s is missing', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False            
        h5file.close()
        return result

    def getDataType(self, productLevel, bandIndex):
        ''' Get data type of image array.

            :param productLevel: [L1C | L2A | L3].
            :type productLevel: str
            :param bandIndex: the band index.
            :type bandIndex: unsigned int
            :return: data type.          
            :rtype: str
            
        '''
        self.verifyProductId(productLevel)
        bandName = self.getBandNameFromIndex(bandIndex)
        try:
            h5file = open_file(self._imageDatabase)
            node = h5file.getNode('/' + productLevel, bandName)
            result = node.dtype
        except NoSuchNodeError:
            self.config.logger.debug('%s: Band %s is missing', productLevel, self.getBandNameFromIndex(bandIndex))
            result = False            
        h5file.close()
        return result
        
    def mapDataType(self, dtIn):
        if(dtIn == uint8):
            dtOut = UInt8Atom()
        elif(dtIn == uint16):
            dtOut = UInt16Atom()
        elif(dtIn == int16):
            dtOut = Int16Atom()
        elif(dtIn == uint32):
            dtOut = UInt32Atom()
        elif(dtIn == int32):
            dtOut = Int32Atom()
        elif(dtIn == float32):
            dtOut = Float32Atom()
        elif(dtIn == float64):
            dtOut = Float64Atom()
        elif(dtIn == GDT_Byte):
            dtOut = UInt8Atom()
        elif(dtIn == GDT_UInt16):
            dtOut = UInt16Atom()
        elif(dtIn == GDT_Int16):
            dtOut = Int16Atom()
        elif(dtIn == GDT_UInt32):
            dtOut = UInt32Atom()
        elif(dtIn == GDT_Int32):
            dtOut = Int32Atom()
        elif(dtIn == GDT_Float32):
            dtOut = Float32Atom()
        elif(dtIn == GDT_Float64):
            dtOut = Float64Atom()

        return dtOut


