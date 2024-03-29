''' 
    :Module: L3_Display.
    :Imports: L3_Config, L3_Tables, L3_Library.
'''
from numpy import *
import pylab as P
from scipy.stats import itemfreq

from L3_Config import L3_Config
from L3_Tables import L3_Tables
from L3_Library import stdoutWrite, stderrWrite, showImage
from boto.glacier.writer import resume_file_upload

class L3_Display(object):
    ''' A support class, for display of the scene classification and the mosaic map 
        using the Python Image Library (PIL). The display of the data is a configurable option.
        
            :param config: the config object for the current tile (via __init__).
            :type config: a reference to the L3_Config object.

    '''
    def __init__(self, config):
        self._config = config
        self._tables = None
        self._noData = config.classifier['NO_DATA']
        self._minTime = config.minTime
        self._maxTime = config.maxTime
        self._plot = P
        self._plot.ion()
     
    def displayData(self, tables):
        ''' Performs the display of the scene classification and the mosaic map.
        
            :param tables: the config object for the current tile (via __init__).
            :type config: a reference to the L3_Tables object.

        '''
        self._tables = tables
        mosaic = self._tables.getBand('L3', self._tables.MSC)
        scenec = self._tables.getBand('L3', self._tables.SCL)
        fig = self._plot.figure()
        fig.canvas.set_window_title(self._config.product.L2A_TILE_ID)   
        ax1 = self._plot.subplot2grid((2,2), (0,0)) 
        ax2 = self._plot.subplot2grid((2,2), (1,0)) 
        ax3 = self._plot.subplot2grid((2,2), (0,1))
        ax4 = self._plot.subplot2grid((2,2), (1,1))            
        
        validData = [mosaic != self._noData]
        idxMoif = itemfreq(mosaic[validData])[:,0]
        nClasses = idxMoif.max()
        xMoif = arange(1,nClasses+1)
        yMoif = zeros(nClasses, dtype=float32)
        yMoif[idxMoif-1] = itemfreq(mosaic[validData])[:,1]
        yMoifCount = float32(yMoif.sum())
        yMoif = yMoif.astype(float32)/yMoifCount * 100.0
        scenecData = [scenec != self._noData]
        classes = ('Sat','Dark','ClS','Soil','Veg','Water','LPC','MPC','HPC','Cir','Snw')
        yScif = zeros(len(classes)+1, dtype=float32)
        idxScif = itemfreq(scenec[scenecData])[:,0]
        yScif[idxScif] = itemfreq(scenec[scenecData])[:,1]
        yScifCount = float32(yScif.sum())
        yScif = yScif.astype(float32)/yScifCount * 100.0
        xScif = arange(1,13)                
        if len(xMoif) < 3:
            xticks = [1,2]
            xmax = 3
        else:
            xticks = xMoif
            xmax = xMoif.max()+1
        ax1.imshow(mosaic, interpolation='nearest')
        ax1.set_xticks([0,mosaic.shape[1]])
        ax1.set_yticks([0,mosaic.shape[0]])
        ax1.set_xlabel('Tile Map')
        ax2.imshow(scenec, interpolation='nearest')
        ax2.set_xticks([0,scenec.shape[1]])
        ax2.set_yticks([0,scenec.shape[0]])
        ax2.set_xlabel('Class Map')
        ax3.set_xlim([0, xmax])            
        ax3.set_xticks(xticks)
        ax3.bar(xMoif, yMoif, align='center', alpha=0.4)
        ax3.set_xlabel('Tile [#]')
        ax3.set_ylabel('Frequency [%]')
        ax4.set_xlim([0, 12])
        ax4.bar(xScif, yScif, align='center', alpha=0.4)
        ax4.set_xlabel('Class [#]')
        ax4.set_ylabel('Frequency [%]')
        self._plot.draw()
        self._plot.tight_layout()
        self._plot.show(block=False)             
        self._plot.savefig(tables.L3_Tile_PLT_File, dpi=100)
        return