#Copyright Durham University and Andrew Reeves
#2014

# This file is part of soapy.

#     soapy is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.

#     soapy is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.

#     You should have received a copy of the GNU General Public License
#     along with soapy.  If not, see <http://www.gnu.org/licenses/>.

"""
The Soapy WFS module.


This module contains a number of classes which simulate different adaptive optics wavefront sensor (WFS) types. All wavefront sensor classes can inherit from the base ``WFS`` class. The class provides the methods required to calculate phase over a WFS pointing in a given WFS direction and accounts for Laser Guide Star (LGS) geometry such as cone effect and elongation. This is  If only pupil images (or complex amplitudes) are required, then this class can be used stand-alone.

Example:

    Make configuration objects::

        from soapy import WFS, confParse

        config = confParse.Configurator("config_file.py")
        config.loadSimParams()

    Initialise the wave-front sensor::

        wfs = WFS.WFS(config.sim, config.wfss[0], config.atmos, config.lgss[0], mask)

    Set the WFS scrns (these should be made in advance, perhaps by the :py:mod:`soapy.atmosphere` module). Then run the WFS::

        wfs.scrns = phaseScrnList
        wfs.makePhase()

    Now you can view data from the WFS frame::

        frameEField = wfs.EField


A Shack-Hartmann WFS is also included in the module, this contains further methods to make the focal plane, then calculate the slopes to send to the reconstructor.

Example:
    Using the config objects from above...::

        shWfs = WFS.ShackHartmann(config.sim, config.wfss[0], config.atmos, config.lgss[0], mask)

    As we are using a full WFS with focal plane making methods, the WFS base classes ``frame`` method can be used to take a frame from the WFS::

        slopes = shWfs.frame(phaseScrnList)

    All the data from that WFS frame is available for inspection. For instance, to obtain the electric field across the WFS and the image seen by the WFS detector::

        EField = shWfs.EField
        wfsDetector = shWfs.wfsDetectorPlane


Adding new WFSs
^^^^^^^^^^^^^^^

New WFS classes should inherit the ``WFS`` class, then create methods which deal with creating the focal plane and making a measurement from it. To make use of the base-classes ``frame`` method, which will run the WFS entirely, the new class must contain the following methods::

    calcFocalPlane(self)
    makeDetectorPlane(self)
    calculateSlopes(self)

The Final ``calculateSlopes`` method must set ``self.slopes`` to be the measurements made by the WFS. If LGS elongation is to be used for the new WFS, create a ``detectorPlane``, which is added to for each LGS elongation propagation. Have a look at the code for the ``Shack-Hartmann`` and experimental ``Pyramid`` WFSs to get some ideas on how to do this.


:Author:
    Andrew Reeves
"""

import numpy
import numpy.random
from scipy.interpolate import interp2d
try:
    from astropy.io import fits
except ImportError:
    try:
        import pyfits as fits
    except ImportError:
        raise ImportError("PyAOS requires either pyfits or astropy")

from . import AOFFT, aoSimLib, LGS, logger
from .tools import centroiders
from .opticalPropagationLib import angularSpectrum

# xrange now just "range" in python3.
# Following code means fastest implementation used in 2 and 3
try:
    xrange
except NameError:
    xrange = range

# The data type of data arrays (complex and real respectively)
CDTYPE = numpy.complex64
DTYPE = numpy.float32


class WFS(object):
    ''' A  WFS class.

        This is a base class which contains methods to initialise the WFS,
        and calculate the phase across the WFSs input aperture, given the WFS
        guide star geometry.

        Parameters:
            simConfig (confObj): The simulation configuration object
            wfsConfig (confObj): The WFS configuration object
            atmosConfig (confObj): The atmosphere configuration object
            lgsConfig (confObj): The Laser Guide Star configuration
            mask (ndarray, optional): An array or size (simConfig.pupilSize, simConfig.pupilSize) which is 1 at the telescope aperture and 0 else-where.
    '''

    def __init__(
            self, simConfig, wfsConfig, atmosConfig, lgsConfig=None,
            mask=None):

        self.simConfig = simConfig
        self.wfsConfig = wfsConfig
        self.atmosConfig = atmosConfig
        self.lgsConfig = lgsConfig

        self.iMat = False

        # Set from knowledge of atmosphere module
        # self.phsWvl = 500e-9 #Notrequired as phase in nanometers now

        WFS.setMask(self, mask)

        self.calcInitParams()

        # If GS not at infinity, find meta-pupil radii for each layer
        if self.wfsConfig.GSHeight != 0:
            self.radii = self.findMetaPupilSize(self.wfsConfig.GSHeight)
        else:
            self.radii = None

        # Choose propagation method
        if wfsConfig.propagationMode == "physical":
            self.makePhase = self.makePhasePhysical
            self.physEField = numpy.zeros(
                (self.simConfig.pupilSize,)*2, dtype=CDTYPE)
        else:
            self.makePhase = self.makePhaseGeo

        # Init LGS, FFTs and allocate some data arrays
        self.initFFTs()
        if self.lgsConfig and self.wfsConfig.lgs:
            self.initLGS()
        self.allocDataArrays()

        self.calcTiltCorrect()
        self.getStatic()


    def setMask(self, mask):
        # If supplied use the mask
        if numpy.any(mask):
            self.mask = mask
        else:
            self.mask = aoSimLib.circle(
                    self.simConfig.pupilSize/2., self.simConfig.simSize,
                    )


############################################################
# Initialisation routines
    def calcInitParams(self):

        self.telDiam = self.simConfig.pupilSize/self.simConfig.pxlScale

        # Convert phase deviation to radians at wfs wavelength.
        # (in nm remember...)
        self.phs2Rad = 2*numpy.pi/(self.wfsConfig.wavelength * 10**9)

        # These are the coordinates of the sub-scrn to cut from the phase scrns
        # For each scrn height they will be edited per
        self.scrnCoords = numpy.arange(self.simConfig.scrnSize)

        # This is the size of the phase to cut out
        # Usually, it will be ``simSize``, but WFSs may require something different
        self.phaseSize = self.simConfig.simSize

    def initFFTs(self):
        pass

    def allocDataArrays(self):
        """
        Allocate the data arrays the WFS will require

        Determines and allocates the various arrays the WFS will require to
        avoid having to re-alloc memory during the running of the WFS and
        keep it fast. This includes arrays for phase
        and the E-Field across the WFS
        """

        self.wfsPhase = numpy.zeros([self.phaseSize]*2, dtype=DTYPE)
        self.EField = numpy.zeros([self.phaseSize]*2, dtype=CDTYPE)

    def initLGS(self):
        """
        Initialises tithe LGS objects for the WFS

        Creates and initialises the LGS objects if the WFS GS is a LGS. This
        included calculating the phases additions which are required if the
        LGS is elongated based on the depth of the elongation and the launch
        position. Note that if the GS is at infinity, elongation is not possible
        and a warning is logged.
        """

        # Choose the correct LGS object, either with physical or geometric
        # or geometric propagation.
        if self.lgsConfig.uplink:
            if  (self.lgsConfig.propagationMode=="phys" or
                    self.lgsConfig.propagationMode=="physical"):
                self.LGS = LGS.PhysicalLGS( self.simConfig, self.wfsConfig,
                                            self.lgsConfig, self.atmosConfig
                                            )
            else:
                self.LGS = LGS.GeometricLGS( self.simConfig, self.wfsConfig,
                                             self.lgsConfig, self.atmosConfig
                                             )

        else:
            self.LGS = None

        self.lgsLaunchPos = None
        self.elong = 0
        self.elongLayers = 0
        if self.wfsConfig.lgs:
            self.lgsLaunchPos = self.lgsConfig.launchPosition
            # LGS Elongation##############################
            if (self.wfsConfig.GSHeight!=0 and
                    self.lgsConfig.elongationDepth!=0):
                self.elong = self.lgsConfig.elongationDepth
                self.elongLayers = self.lgsConfig.elongationLayers

                # Get Heights of elong layers
                self.elongHeights = numpy.linspace(
                    self.wfsConfig.GSHeight-self.elong/2.,
                    self.wfsConfig.GSHeight+self.elong/2.,
                    self.elongLayers
                    )

                # Calculate the zernikes to add
                self.elongZs = aoSimLib.zernikeArray([2,3,4], self.simConfig.pupilSize)

                # Calculate the radii of the metapupii at for different elong
                # Layer heights
                # Also calculate the required phase addition for each layer
                self.elongRadii = {}
                self.elongPos = {}
                self.elongPhaseAdditions = numpy.zeros(
                    (self.elongLayers, self.phaseSize, self.phaseSize))
                for i in xrange(self.elongLayers):
                    self.elongRadii[i] = self.findMetaPupilSize(
                                                float(self.elongHeights[i]))
                    self.elongPhaseAdditions[i] = self.calcElongPhaseAddition(i)
                    self.elongPos[i] = self.calcElongPos(i)

            # If GS at infinity cant do elongation
            elif (self.wfsConfig.GSHeight==0 and
                    self.lgsConfig.elongationDepth!=0):
                logger.warning("Not able to implement LGS Elongation as GS at infinity")

    def calcTiltCorrect(self):
        pass

    def getStatic(self):
        self.staticData = None

    def findMetaPupilSize(self, GSHeight):
        '''
        Evaluates the sizes of the effective metePupils
        at each screen height if an GS of finite height is used.

        Parameters:
            GSHeight (float): The height of the GS in metres

        Returns:
            dict : A dictionary containing the radii of a meta-pupil at each screen height
        '''

        radii={}

        for i in xrange(self.atmosConfig.scrnNo):
            #Find radius of metaPupil geometrically (fraction of pupil at
            # Ground Layer)
            radius = (self.simConfig.pupilSize/2.) * (
                    1-(float(self.atmosConfig.scrnHeights[i])/GSHeight))
            radii[i]= radius

            #If scrn is above LGS, radius is 0
            if self.atmosConfig.scrnHeights[i]>=GSHeight:
                radii[i]=0

        return radii


    def calcElongPhaseAddition(self, elongLayer):
        """
        Calculates the phase required to emulate layers on an elongated source

        For each 'elongation layer' a phase addition is calculated which
        accounts for the difference in height from the nominal GS height where
        the WFS is focussed, and accounts for the tilt seen if the LGS is
        launched off-axis.

        Parameters:
            elongLayer (int): The number of the elongation layer

        Returns:
            ndarray: The phase addition required for that layer.
        """

        # Calculate the path difference between the central GS height and the
        # elongation "layer"
        # Define these to make it easier
        h = self.elongHeights[elongLayer]
        dh = h - self.wfsConfig.GSHeight
        H = self.lgsConfig.height
        d = numpy.array(self.lgsLaunchPos).astype('float32') * self.telDiam/2.
        D = self.telDiam
        theta = (d.astype("float")/H) - self.wfsConfig.GSPosition

        # for the focus terms....
        focalPathDiff = (2*numpy.pi/self.wfsConfig.wavelength) * ( (
            ( (self.telDiam/2.)**2 + (h**2) )**0.5\
          - ( (self.telDiam/2.)**2 + (H)**2 )**0.5 ) - dh )

        # For tilt terms.....
        tiltPathDiff = (2*numpy.pi/self.wfsConfig.wavelength) * (
            numpy.sqrt( (dh+H)**2. + ( (dh+H)*theta-d-D/2.)**2 )
            + numpy.sqrt( H**2 + (D/2. - d + H*theta)**2 )
            - numpy.sqrt( H**2 + (H*theta - d - D/2.)**2)
            - numpy.sqrt( (dh+H)**2 + (D/2. - d + (dh+H)*theta )**2 )    )


        phaseAddition = numpy.zeros(
                    (self.simConfig.pupilSize, self.simConfig.pupilSize))

        phaseAddition +=( (self.elongZs[2]/self.elongZs[2].max())
                             * focalPathDiff )
        # X,Y tilt
        phaseAddition += ( (self.elongZs[0]/self.elongZs[0].max())
                            *tiltPathDiff[0] )
        phaseAddition += ( (self.elongZs[1]/self.elongZs[1].max())
                            *tiltPathDiff[1])

        pad = ((self.simConfig.simPad,)*2, (self.simConfig.simPad,)*2)
        phaseAddition = numpy.pad(phaseAddition, pad, mode="constant")

        phaseAddition = aoSimLib.zoom(phaseAddition, self.phaseSize)

        return phaseAddition

    def calcElongPos(self, elongLayer):
        """
        Calculates the difference in GS position for each elongation layer
        only makes a difference if LGS launched off-axis

        Parameters:
            elongLayer (int): which elongation layer

        Returns:
            float: The effect position of that layer GS
        """

        h = self.elongHeights[elongLayer]       #height of elonglayer
        dh = h-self.wfsConfig.GSHeight          #delta height from GS Height
        H = self.wfsConfig.GSHeight               #Height of GS

        #Position of launch in m
        xl = numpy.array(self.lgsLaunchPos) * self.telDiam/2.

        #GS Pos in radians
        GSPos=numpy.array(self.wfsConfig.GSPosition)*numpy.pi/(3600.0*180.0)

        #difference in angular Pos for that height layer in rads
        theta_n = GSPos - ((dh*xl)/ (H*(H+dh)))

        return theta_n

#############################################################

#############################################################
# Phase stacking routines for a WFS frame

    def getMetaPupilPos(self, height, GSPos=None):
        '''
        Finds the centre of a metapupil at a given height,
        when offset by a given angle in arsecs, in metres from the ()

        Arguments:
            height (float): Height of the layer in metres
            GSPos (tuple, optional):  The angular position of the GS in radians.
                                    If not set, will use the WFS position

        Returns:
            ndarray: The position of the centre of the metapupil in metres
        '''
        # if no GSPos given, use system pos and convert into radians
        if not numpy.any(GSPos):
            GSPos = (   numpy.array(self.wfsConfig.GSPosition)
                        *numpy.pi/(3600.0*180.0) )

        # Position of centre of GS metapupil off axis at required height
        GSCent = (numpy.tan(GSPos) * height)

        return GSCent

    def getMetaPupilPhase(  self, scrn, height, radius=None, simSize=None,
                            GSPos=None):
        '''
        Returns the phase across a metaPupil at some height and angular
        offset in arcsec. Interpolates phase to size of the pupil if cone
        effect is required

        Parameters:
            scrn (ndarray): An array representing the phase screen
            height (float): Height of the phase screen
            radius (float, optional): Radius of the meta-pupil. If not set, will use system pupil size.
            simSize (ndarray, optional): Size of screen to return. If not set, will use system pupil size.
            GSPos (tuple, optional): Angular position of guide star. If not set will use system position.

        Return:
            ndarray: The meta pupil at the specified height
        '''

        # If no size of metapupil given, use system pupil size
        if not simSize:
            simSize = self.simConfig.simSize

        # If the radius is 0, then 0 phase is returned
        if radius==0:
            return numpy.zeros((simSize, simSize))


        GSCent = self.getMetaPupilPos(height, GSPos) * self.simConfig.pxlScale

        logger.debug("GSCent {}".format(GSCent))
        scrnX, scrnY = scrn.shape

        # If the GS is not at infinity, take into account cone effect
        if self.wfsConfig.GSHeight!=0:
            fact = float(2*radius)/self.simConfig.pupilSize
        else:
            fact=1

        x1 = scrnX/2. + GSCent[0] - fact*simSize/2.0
        x2 = scrnX/2. + GSCent[0] + fact*simSize/2.0
        y1 = scrnY/2. + GSCent[1] - fact*simSize/2.0
        y2 = scrnY/2. + GSCent[1] + fact*simSize/2.0

        logger.debug("WFS Scrn Coords - ({0}:{1}, {2}:{3})".format(
                x1,x2,y1,y2))

        if ( x1 < 0 or x2 > scrnX or y1 < 0 or y2 > scrnY):
            raise ValueError(
                    "GS separation requires larger screen size. \nheight: {3}, GSCent: {0}, scrnSize: {1}, simSize: {2}".format(
                            GSCent, scrn.shape, simSize, height) )

        # Must interpolate. -1 as linspace goes to number
        xCoords = numpy.linspace(x1, x2-1, self.phaseSize)
        yCoords = numpy.linspace(y1, y2-1, self.phaseSize)
        # print(xCoords)
        # print(yCoords)
        interpObj = interp2d(
                self.scrnCoords, self.scrnCoords, scrn, copy=False)
        metaPupil = interpObj(xCoords, yCoords)

        return metaPupil

    def makePhaseGeo(self, radii=None, GSPos=None):
        '''
        Creates the total phase on a wavefront sensor which
        is offset by a given angle

        Parameters
            radii (dict, optional): Radii of each meta pupil of each screen height in pixels. If not given uses pupil radius.
            GSPos (dict, optional): Position of GS in pixels. If not given uses GS position
        '''

        for i in self.scrns:
            logger.debug("Layer: {}".format(i))
            if radii:
                phase = self.getMetaPupilPhase(
                            self.scrns[i], self.atmosConfig.scrnHeights[i],
                            radius=radii[i], GSPos=GSPos)
            else:
                phase = self.getMetaPupilPhase(
                            self.scrns[i], self.atmosConfig.scrnHeights[i],
                            GSPos=GSPos)

            self.wfsPhase += phase

        self.EField[:] = numpy.exp(1j*self.wfsPhase)


    def makePhasePhysical(self, radii=None, GSPos=None):
        '''
        Finds total WFS complex amplitude by propagating light down
        phase scrns

        Parameters
            radii (dict, optional): Radii of each meta pupil of each screen height in pixels. If not given uses pupil radius.
            GSPos (dict, optional): Position of GS in pixels. If not given uses GS position.
        '''

        scrnNo = len(self.scrns)-1  #Number of layers (0 indexed)
        ht = self.atmosConfig.scrnHeights[scrnNo] #Height of highest layer
        delta = (self.simConfig.pxlScale)**-1. #Grid spacing for propagation

        #Get initial Phase for highest scrn and turn to efield
        if radii:
            phase1 = self.getMetaPupilPhase(
                        self.scrns[scrnNo], ht, radius=radii[scrnNo],
                        GSPos=GSPos)
                        #pupilSize=2*self.simConfig.pupilSize, GSPos=GSPos )
        else:
            phase1 = self.getMetaPupilPhase(self.scrns[scrnNo], ht,
                        GSPos=GSPos)
                        #pupilSize=2*self.simConfig.pupilSize, GSPos=GSPos)

        self.EField[:] = numpy.exp(1j*phase1)
        #Loop through remaining scrns in reverse order - update ht accordingly
        for i in range(scrnNo)[::-1]:
            #Get propagation distance for this layer
            z = ht - self.atmosConfig.scrnHeights[i]
            ht -= z
            #Do ASP for last layer to next
            self.EField[:] = angularSpectrum(
                        self.EField, self.wfsConfig.wavelength,
                        delta, delta, z )

            # Get phase for this layer
            if radii:
                phase = self.getMetaPupilPhase(
                            self.scrns[i], self.atmosConfig.scrnHeights[i],
                            radius=radii[i], GSPos=GSPos)
            else:
                phase = self.getMetaPupilPhase(
                            self.scrns[i], self.atmosConfig.scrnHeights[i],
                            GSPos=GSPos)

            #Add add phase from this layer
            self.EField *= numpy.exp(1j*phase)

        #If not already at ground, propagate the rest of the way.
        if self.atmosConfig.scrnHeights[0]!=0:
            self.EField[:] = angularSpectrum(
                    self.EField, self.wfsConfig.wavelength,
                    delta, delta, ht
                    )
######################################################

    def zeroPhaseData(self):
        self.EField[:] = 0
        self.wfsPhase[:] = 0


    def frame(self, scrns, correction=None, read=True, iMatFrame=False):
        '''
        Runs one WFS frame

        Runs a single frame of the WFS with a given set of phase screens and
        some optional correction. If elongation is set, will run the phase
        calculating and focal plane making methods multiple times for a few
        different heights of LGS, then sum these onto a ``wfsDetectorPlane``.

        Parameters:
            scrns (list): A list or dict containing the phase screens
            correction (ndarray, optional): The correction term to take from the phase screens before the WFS is run.
            read (bool, optional): Should the WFS be read out? if False, then WFS image is calculated but slopes not calculated. defaults to True.
            iMatFrame (bool, optional): If True, will assume an interaction matrix is being measured. Turns off some AO loop features before running

        Returns:
            ndarray: WFS Measurements
        '''

       #If iMatFrame, turn off unwanted effects
        if iMatFrame:
            self.iMat = True
            removeTT = self.wfsConfig.removeTT
            self.wfsConfig.removeTT = False
            if self.wfsConfig.lgs:
                elong = self.elong
            self.elong = 0
            photonNoise = self.wfsConfig.photonNoise
            self.wfsConfig.photonNoise = False
            eReadNoise = self.wfsConfig.eReadNoise
            self.wfsConfig.eReadNoise = 0


        # If scrns is not dict or list, assume array and put in list
        t = type(scrns)
        if t!=dict and t!=list:
            scrns = [scrns]

        self.zeroData(detector=read, inter=False)

        self.scrns = {}
        # Scale phase to WFS wvl
        for i in xrange(len(scrns)):
            self.scrns[i] = scrns[i].copy()*self.phs2Rad
        # If there is correction, scale that too
        if numpy.any(correction):
            correction = correction.copy()*self.phs2Rad

        # If LGS elongation simulated
        #################################
        if self.wfsConfig.lgs and self.elong!=0:
            for i in xrange(self.elongLayers):
                self.zeroPhaseData()

                self.makePhase(self.elongRadii[i], self.elongPos[i])
                self.uncorrectedPhase = self.wfsPhase.copy()/self.phs2Rad
                self.EField *= numpy.exp(1j*self.elongPhaseAdditions[i])
                if numpy.any(correction):
                    self.EField *= numpy.exp(-1j*correction)
                self.calcFocalPlane(self.lgsConfig.naProfile[i])
        ##################################

        # If no elongation
        #################################
        else:
            # If imat frame, dont want to make it off-axis
            if iMatFrame:
                try:
                    iMatPhase = aoSimLib.zoom(
                            self.scrns[0], self.phaseSize, order=1)
                    self.wfsPhase[:] = iMatPhase
                    self.EField[:] = numpy.exp(1j*iMatPhase)
                except ValueError:
                    raise ValueError("If iMat Frame, scrn must be ``simSize``")
            else:
                self.makePhase(self.radii)

            # Apply DM correction
            # (first make copy of uncorrected Phase for plotting)
            self.uncorrectedPhase = self.wfsPhase.copy()/self.phs2Rad
            if numpy.any(correction):
                if correction.shape!=(self.phaseSize, self.phaseSize):
                    correction = aoSimLib.zoom(
                        correction, self.phaseSize, order=1)
                self.wfsPhase -= correction
                self.EField *= numpy.exp(-1j*correction)
            self.calcFocalPlane()
        ##################################

        if read:
            self.makeDetectorPlane()
            self.calculateSlopes()
            self.zeroData(detector=False)

        #Turn back on stuff disabled for iMat
        if iMatFrame:
            self.iMat=False
            self.wfsConfig.removeTT = removeTT
            if self.wfsConfig.lgs:
                self.elong = elong
            self.wfsConfig.photonNoise = photonNoise
            self.wfsConfig.eReadNoise = eReadNoise

        # Check that slopes aint `nan`s. Set to 0 if so
        if numpy.any(numpy.isnan(self.slopes)):
            self.slopes[numpy.isnan(self.slopes)] = 0

        return self.slopes

    def addPhotonNoise(self):
        """
        Add photon noise to ``wfsDetectorPlane`` using ``numpy.random.poisson``
        """
        self.wfsDetectorPlane = numpy.random.poisson(
                self.wfsDetectorPlane).astype(DTYPE)


    def addReadNoise(self):
        """
        Adds read noise to ``wfsDetectorPlane using ``numpy.random.normal``.
        This generates a normal (guassian) distribution of random numbers to
        add to the detector. Any CCD bias is assumed to have been removed, so
        the distribution is centred around 0. The width of the distribution
        is determined by the value `eReadNoise` set in the WFS configuration.
        """
        self.wfsDetectorPlane += numpy.random.normal(
                0, self.wfsConfig.eReadNoise, self.wfsDetectorPlane.shape
                )


    def calcFocalPlane(self):
        pass

    def makeDetectorPlane(self):
        pass

    def LGSUplink(self):
        pass

    def calculateSlopes(self):
        self.slopes = self.EField.copy()

    def zeroData(self, detector=True, inter=True):
        self.zeroPhaseData()

#   _____ _   _
#  /  ___| | | |
#  \ `--.| |_| |
#   `--. \  _  |
#  /\__/ / | | |
#  \____/\_| |_/
class ShackHartmann(WFS):
    """Class to simulate a Shack-Hartmann WFS"""

    def calcInitParams(self):
        """
        Calculate some parameters to be used during initialisation
        """
        super(ShackHartmann, self).calcInitParams()

        self.subapFOVrad = self.wfsConfig.subapFOV * numpy.pi / (180. * 3600)
        self.subapDiam = self.telDiam/self.wfsConfig.nxSubaps

        # spacing between subaps in pupil Plane (size "pupilSize")
        self.PPSpacing = float(self.simConfig.pupilSize)/self.wfsConfig.nxSubaps

        # Spacing on the "FOV Plane" - the number of elements required
        # for the correct subap FOV (from way FFT "phase" to "image" works)
        self.subapFOVSpacing = numpy.round(self.subapDiam
                                * self.subapFOVrad/ self.wfsConfig.wavelength)

        # make twice as big to double subap FOV
        if self.wfsConfig.subapFieldStop==True:
            self.SUBAP_OVERSIZE = 1
        else:
            self.SUBAP_OVERSIZE = 2

        self.detectorPxls = self.wfsConfig.pxlsPerSubap*self.wfsConfig.nxSubaps
        self.subapFOVSpacing *= self.SUBAP_OVERSIZE
        self.wfsConfig.pxlsPerSubap2 = (self.SUBAP_OVERSIZE
                                            *self.wfsConfig.pxlsPerSubap)

        self.scaledEFieldSize =int(round(
                self.wfsConfig.nxSubaps*self.subapFOVSpacing*
                (float(self.simConfig.simSize)/self.simConfig.pupilSize)
                ))
        self.phaseSize = self.scaledEFieldSize

        # Calculate the subaps which are actually seen behind the pupil mask
        self.findActiveSubaps()

        # For correlation centroider, open reference image.
        if self.wfsConfig.centMethod=="correlation":
            rawRef = fits.open("./conf/correlationRef/"+self.wfsConfig.referenceImage)[0].data
            self.wfsConfig.referenceImage = numpy.zeros((self.activeSubaps,
                    self.wfsConfig.pxlsPerSubap, self.wfsConfig.pxlsPerSubap))
            for i in range(self.activeSubaps):
                self.wfsConfig.referenceImage[i] = rawRef[
                        self.detectorSubapCoords[i, 0]:
                        self.detectorSubapCoords[i, 0]+self.wfsConfig.pxlsPerSubap,
                        self.detectorSubapCoords[i, 1]:
                        self.detectorSubapCoords[i, 1]+self.wfsConfig.pxlsPerSubap]


    def findActiveSubaps(self):
        '''
        Finds the subapertures which are not empty space
        determined if mean of subap coords of the mask is above threshold.

        '''

        mask = self.mask[
                self.simConfig.simPad : -self.simConfig.simPad,
                self.simConfig.simPad : -self.simConfig.simPad
                ]
        self.subapCoords, self.subapFillFactor = aoSimLib.findActiveSubaps(
                self.wfsConfig.nxSubaps, mask,
                self.wfsConfig.subapThreshold, returnFill=True)

        self.activeSubaps = self.subapCoords.shape[0]
        self.detectorSubapCoords = numpy.round(
                self.subapCoords*(
                        self.detectorPxls/float(self.simConfig.pupilSize) ) )

        self.setMask(self.mask)

    def setMask(self, mask):
        super(ShackHartmann, self).setMask(mask)

        # Find the mask to apply to the scaled EField
        self.scaledMask = numpy.round(aoSimLib.zoom(
                    self.mask, self.scaledEFieldSize))

        p = self.simConfig.simPad
        self.subapFillFactor = aoSimLib.computeFillFactor(
                self.mask[p:-p, p:-p],
                self.subapCoords,
                round(float(self.simConfig.pupilSize)/self.wfsConfig.nxSubaps)
                )


    def initFFTs(self):
        """
        Initialise the FFT Objects required for running the WFS

        Initialised various FFT objects which are used through the WFS,
        these include FFTs to calculate focal planes, and to convolve LGS
        PSFs with the focal planes
        """

        #Calculate the FFT padding to use
        self.subapFFTPadding = self.wfsConfig.pxlsPerSubap2 * self.wfsConfig.fftOversamp
        if self.subapFFTPadding < self.subapFOVSpacing:
            while self.subapFFTPadding<self.subapFOVSpacing:
                self.wfsConfig.fftOversamp+=1
                self.subapFFTPadding\
                        =self.wfsConfig.pxlsPerSubap2*self.wfsConfig.fftOversamp

            logger.warning("requested WFS FFT Padding less than FOV size... Setting oversampling to: %d"%self.wfsConfig.fftOversamp)

        #Init the FFT to the focal plane
        self.FFT = AOFFT.FFT(
                inputSize=(
                self.activeSubaps, self.subapFFTPadding, self.subapFFTPadding),
                axes=(-2,-1), mode="pyfftw",dtype=CDTYPE,
                THREADS=self.wfsConfig.fftwThreads,
                fftw_FLAGS=(self.wfsConfig.fftwFlag,"FFTW_DESTROY_INPUT"))

        #If LGS uplink, init FFTs to conovolve LGS PSF and WFS PSF(s)
        #This works even if no lgsConfig.uplink as ``and`` short circuits
        if self.lgsConfig and self.lgsConfig.uplink:
            self.iFFT = AOFFT.FFT(
                    inputSize = (self.activeSubaps,
                                        self.subapFFTPadding,
                                        self.subapFFTPadding),
                    axes=(-2,-1), mode="pyfftw",dtype=CDTYPE,
                    THREADS=self.wfsConfig.fftwThreads,
                    fftw_FLAGS=(self.wfsConfig.fftwFlag,"FFTW_DESTROY_INPUT")
                    )

            self.lgs_iFFT = AOFFT.FFT(
                    inputSize = (self.subapFFTPadding,
                                self.subapFFTPadding),
                    axes=(0,1), mode="pyfftw",dtype=CDTYPE,
                    THREADS=self.wfsConfig.fftwThreads,
                    fftw_FLAGS=(self.wfsConfig.fftwFlag,"FFTW_DESTROY_INPUT")
                    )

    def allocDataArrays(self):
        """
        Allocate the data arrays the WFS will require

        Determines and allocates the various arrays the WFS will require to
        avoid having to re-alloc memory during the running of the WFS and
        keep it fast.
        """

        super(ShackHartmann,self).allocDataArrays()

        self.subapArrays=numpy.zeros((self.activeSubaps,
                                      self.subapFOVSpacing,
                                      self.subapFOVSpacing),
                                     dtype=CDTYPE)
        self.binnedFPSubapArrays = numpy.zeros( (self.activeSubaps,
                                                self.wfsConfig.pxlsPerSubap2,
                                                self.wfsConfig.pxlsPerSubap2),
                                                dtype=DTYPE)
        self.FPSubapArrays = numpy.zeros((self.activeSubaps,
                                          self.subapFFTPadding,
                                          self.subapFFTPadding),dtype=DTYPE)

        self.wfsDetectorPlane = numpy.zeros( (  self.detectorPxls,
                                                self.detectorPxls   ),
                                                dtype = DTYPE )
        #Array used when centroiding subaps
        self.centSubapArrays = numpy.zeros( (self.activeSubaps,
              self.wfsConfig.pxlsPerSubap, self.wfsConfig.pxlsPerSubap) )

        self.slopes = numpy.zeros( 2*self.activeSubaps )

    def initLGS(self):
        super(ShackHartmann, self).initLGS()
        #Tell the LGS a bit about the WFS
        #(TODO-get rid of this and put into LGS object init)
        if self.LGS:
            self.LGS.setWFSParams(
                    self.SUBAP_OVERSIZE*self.subapFOVrad,
                    self.wfsConfig.fftOversamp, self.subapFFTPadding)


    def calcTiltCorrect(self):
        """
        Calculates the required tilt to add to avoid the PSF being centred on
        only 1 pixel
        """
        if not self.wfsConfig.pxlsPerSubap%2:
            # If pxlsPerSubap is even
            # Angle we need to correct for half a pixel
            theta = self.SUBAP_OVERSIZE*self.subapFOVrad/ (
                    2*self.subapFFTPadding)

            # Magnitude of tilt required to get that angle
            A = theta * self.subapDiam/(2*self.wfsConfig.wavelength)*2*numpy.pi

            # Create tilt arrays and apply magnitude
            coords = numpy.linspace(-1, 1, self.subapFOVSpacing)
            X,Y = numpy.meshgrid(coords,coords)

            self.tiltFix = -1 * A * (X+Y)

        else:
            self.tiltFix = numpy.zeros((self.subapFOVSpacing,)*2)

    def getStatic(self):
        """
        Computes the static measurements, i.e., slopes with flat wavefront
        """

        self.staticData = None

        #Make flat wavefront, and run through WFS in iMat mode to turn off features
        phs = numpy.zeros([self.simConfig.simSize]*2).astype(DTYPE)
        self.staticData = self.frame(
                phs, iMatFrame=True).copy().reshape(2,self.activeSubaps)
#######################################################################


    def zeroData(self, detector=True, inter=True):
        """
        Sets data structures in WFS to zero.

        Parameters:
            detector (bool, optional): Zero the detector? default:True
            inter (bool, optional): Zero intermediate arrays? default: True
        """

        self.zeroPhaseData()

        if inter:
            self.FPSubapArrays[:] = 0

        if detector:
            self.wfsDetectorPlane[:] = 0


    def calcFocalPlane(self, intensity=1):
        '''
        Calculates the wfs focal plane, given the phase across the WFS

        Parameters:
            intensity (float): The relative intensity of this frame, is used when multiple WFS frames taken for extended sources.
        '''

        # Apply the scaled pupil mask
        self.EField *= self.scaledMask
        # Now cut out only the eField across the pupilSize
        coord = round(int(((self.scaledEFieldSize/2.)
                - (self.wfsConfig.nxSubaps*self.subapFOVSpacing)/2.)))
        self.cropEField = self.EField[coord:-coord, coord:-coord]

        #create an array of individual subap EFields
        for i in xrange(self.activeSubaps):
            x,y = numpy.round(self.subapCoords[i] *
                                     self.subapFOVSpacing/self.PPSpacing)
            self.subapArrays[i] = self.cropEField[
                                    int(x):
                                    int(x+self.subapFOVSpacing) ,
                                    int(y):
                                    int(y+self.subapFOVSpacing)]

        #do the fft to all subaps at the same time
        # and convert into intensity
        self.FFT.inputData[:] = 0
        self.FFT.inputData[:,:int(round(self.subapFOVSpacing))
                        ,:int(round(self.subapFOVSpacing))] \
                = self.subapArrays*numpy.exp(1j*(self.tiltFix))


        if intensity==1:
            self.FPSubapArrays += numpy.abs(AOFFT.ftShift2d(self.FFT()))**2
        else:
            self.FPSubapArrays += intensity*numpy.abs(
                    AOFFT.ftShift2d(self.FFT()))**2


    def makeDetectorPlane(self):
        '''
        Scales and bins intensity data onto the detector with a given number of
        pixels.

        If required, will first convolve final PSF with LGS PSF, then bin
        PSF down to detector size. Finally puts back into ``wfsFocalPlane``
        array in correct order.
        '''

        # If required, convolve with LGS PSF
        if self.wfsConfig.lgs and self.LGS and self.lgsConfig.uplink and self.iMat!=True:
            self.LGSUplink()


        # bins back down to correct size and then
        # fits them back in to a focal plane array
        self.binnedFPSubapArrays[:] = aoSimLib.binImgs(self.FPSubapArrays,
                                            self.wfsConfig.fftOversamp)

        # In case of empty sub-aps, will get NaNs
        self.binnedFPSubapArrays[numpy.isnan(self.binnedFPSubapArrays)] = 0

        # Scale each sub-ap flux by sub-aperture fill-factor
        self.binnedFPSubapArrays\
                = (self.binnedFPSubapArrays.T * self.subapFillFactor).T

        for i in xrange(self.activeSubaps):
            x,y=self.detectorSubapCoords[i]

            #Set default position to put arrays into (SUBAP_OVERSIZE FOV)
            x1 = int(round(
                    x+self.wfsConfig.pxlsPerSubap/2.
                    -self.wfsConfig.pxlsPerSubap2/2.))
            x2 = int(round(
                    x+self.wfsConfig.pxlsPerSubap/2.
                    +self.wfsConfig.pxlsPerSubap2/2.))
            y1 = int(round(
                    y+self.wfsConfig.pxlsPerSubap/2.
                    -self.wfsConfig.pxlsPerSubap2/2.))
            y2 = int(round(
                    y+self.wfsConfig.pxlsPerSubap/2.
                    +self.wfsConfig.pxlsPerSubap2/2.))

            #Set defualt size of input array (i.e. all of it)
            x1_fp = int(0)
            x2_fp = int(round(self.wfsConfig.pxlsPerSubap2))
            y1_fp = int(0)
            y2_fp = int(round(self.wfsConfig.pxlsPerSubap2))

            # If at the edge of the field, may only fit a fraction in
            if x == 0:
                x1 = 0
                x1_fp = int(round(
                        self.wfsConfig.pxlsPerSubap2/2.
                        -self.wfsConfig.pxlsPerSubap/2.))

            elif x == (self.detectorPxls-self.wfsConfig.pxlsPerSubap):
                x2 = int(round(self.detectorPxls))
                x2_fp = int(round(
                        self.wfsConfig.pxlsPerSubap2/2.
                        +self.wfsConfig.pxlsPerSubap/2.))

            if y == 0:
                y1 = 0
                y1_fp = int(round(
                        self.wfsConfig.pxlsPerSubap2/2.
                        -self.wfsConfig.pxlsPerSubap/2.))

            elif y == (self.detectorPxls-self.wfsConfig.pxlsPerSubap):
                y2 = int(self.detectorPxls)
                y2_fp = int(round(
                        self.wfsConfig.pxlsPerSubap2/2.
                        +self.wfsConfig.pxlsPerSubap/2.))

            self.wfsDetectorPlane[x1:x2, y1:y2] += (
                    self.binnedFPSubapArrays[i, x1_fp:x2_fp, y1_fp:y2_fp])

        # Scale data for correct number of photons
        self.wfsDetectorPlane /= self.wfsDetectorPlane.sum()
        self.wfsDetectorPlane *= aoSimLib.photonsPerMag(
                self.wfsConfig.GSMag, self.mask, self.simConfig.pxlScale**(-1),
                self.wfsConfig.wvlBandWidth, self.wfsConfig.exposureTime
                ) * self.wfsConfig.throughput

        if self.wfsConfig.photonNoise:
            self.addPhotonNoise()

        if self.wfsConfig.eReadNoise!=0:
            self.addReadNoise()

    def LGSUplink(self):
        '''
        A method to deal with convolving the LGS PSF
        with the subap focal plane.
        '''

        self.LGS.LGSPSF(self.scrns)

        self.lgs_iFFT.inputData[:] = self.LGS.PSF
        self.iFFTLGSPSF = self.lgs_iFFT()

        self.iFFT.inputData[:] = self.FPSubapArrays
        self.iFFTFPSubapsArray = self.iFFT()

        # Do convolution
        self.iFFTFPSubapsArray *= self.iFFTLGSPSF

        # back to Focal Plane.
        self.FFT.inputData[:] = self.iFFTFPSubapsArray
        self.FPSubapArrays[:] = AOFFT.ftShift2d(self.FFT()).real

    def calculateSlopes(self):
        '''
        Calculates WFS slopes from wfsFocalPlane

        Returns:
            ndarray: array of all WFS measurements
        '''

        # Sort out FP into subaps
        for i in xrange(self.activeSubaps):
            x, y = self.detectorSubapCoords[i]
            x = int(x)
            y = int(y)
            self.centSubapArrays[i] = self.wfsDetectorPlane[x:x+self.wfsConfig.pxlsPerSubap,
                                                    y:y+self.wfsConfig.pxlsPerSubap ].astype(DTYPE)

        slopes = eval("centroiders."+self.wfsConfig.centMethod)(
                self.centSubapArrays,
                threshold=self.wfsConfig.centThreshold,
                ref=self.wfsConfig.referenceImage
                     )


        # shift slopes relative to subap centre and remove static offsets
        slopes -= self.wfsConfig.pxlsPerSubap/2.0

        if numpy.any(self.staticData):
            slopes -= self.staticData

        self.slopes[:] = slopes.reshape(self.activeSubaps*2)

        if self.wfsConfig.removeTT == True:
            self.slopes[:self.activeSubaps] -= self.slopes[:self.activeSubaps].mean()
            self.slopes[self.activeSubaps:] -= self.slopes[self.activeSubaps:].mean()

        if self.wfsConfig.angleEquivNoise and not self.iMat:
            pxlEquivNoise = (
                    self.wfsConfig.angleEquivNoise *
                    float(self.wfsConfig.pxlsPerSubap)
                    /self.wfsConfig.subapFOV )
            self.slopes += numpy.random.normal( 0, pxlEquivNoise,
                                                2*self.activeSubaps)

        return self.slopes



class Gradient(WFS):

    def calcInitParams(self):
        super(Gradient, self).calcInitParams()
        self.subapSpacing = self.simConfig.pupilSize/self.wfsConfig.nxSubaps
        self.findActiveSubaps()

        # Normalise gradient measurement to 1 radian
        self.subapDiam = self.telDiam/self.wfsConfig.nxSubaps
        # amp = (1./3600) * (numpy.pi/180) * self.subapDiam/2.
        # amp = numpy.sin(1) * self.subapDiam/2.
        amp = 2.6e-8 * self.subapDiam/self.wfsConfig.wavelength
        # amp = ((2*numpy.pi)/(self.wfsConfig.wavelength*1e9)) * (self.subapDiam/2.) * (numpy.pi/180)* (1./3600) # * (1./self.subapSpacing**2)

        # Arrays to be used for gradient calculation
        coord = numpy.linspace(-amp, amp, self.subapSpacing)
        self.xGrad, self.yGrad = numpy.meshgrid(coord, coord)
        # self.xGrad = self.xGrad**(-1) / self.subapSpacing**2
        # self.yGrad = self.yGrad**(-1) / self.subapSpacing**2

    def findActiveSubaps(self):
        '''
        Finds the subapertures which are not empty space
        determined if mean of subap coords of the mask is above threshold.
        '''
        pupilMask = self.mask[
                self.simConfig.simPad : -self.simConfig.simPad,
                self.simConfig.simPad : -self.simConfig.simPad
                ]
        self.subapCoords, self.subapFillFactor = aoSimLib.findActiveSubaps(
                self.wfsConfig.nxSubaps, pupilMask,
                self.wfsConfig.subapThreshold, returnFill=True)

        self.activeSubaps = self.subapCoords.shape[0]

    def allocDataArrays(self):
        """
        Allocate the data arrays the WFS will require

        Determines and allocates the various arrays the WFS will require to
        avoid having to re-alloc memory during the running of the WFS and
        keep it fast.
        """

        super(Gradient, self).allocDataArrays()

        self.subapArrays=numpy.zeros(
                (self.activeSubaps, self.subapSpacing, self.subapSpacing),
                dtype=DTYPE)

        self.slopes = numpy.zeros(2 * self.activeSubaps)



    def calcFocalPlane(self, intensity=1):
        '''
        Calculates the wfs focal plane, given the phase across the WFS. For this WFS, chops the pupil phase up into sub-apertures.

        Parameters:
            intensity (float): The relative intensity of this frame, is used when multiple WFS frames taken for extended sources.
        '''

        # Apply the scaled pupil mask
        self.wfsPhase *= self.mask

        # Now cut out only the phase across the pupilSize
        coord = self.simConfig.simPad
        self.pupilPhase = self.wfsPhase[coord:-coord, coord:-coord]

        #create an array of individual subap phase
        for i, (x,y) in enumerate(self.subapCoords):
            self.subapArrays[i] = self.pupilPhase[
                    x: x+self.subapSpacing, y: y+self.subapSpacing]


    def makeDetectorPlane(self):
        '''
        Creates a 'detector' image suitable
        '''
        self.wfsDetectorPlane = numpy.zeros((self.wfsConfig.nxSubaps,)*2)

        coords = (self.subapCoords/self.subapSpacing).astype('int')
        self.wfsDetectorPlane[coords[:,0], coords[:,1]] = self.subapArrays.mean((1,2))

    def calculateSlopes(self):
        '''
        Calculates WFS slopes from wfsFocalPlane

        Returns:
            ndarray: array of all WFS measurements
        '''
        # Remove all piston from the sub-apertures
        # self.subapArrays = (self.subapArrays.T-self.subapArrays.mean((1,2))).T

        # Integrate with tilt/tip to get slope measurements
        for i, subap in enumerate(self.subapArrays):
            subap -= subap.mean()
            self.slopes[i] = (subap * self.xGrad).sum()
            self.slopes[i+self.activeSubaps] = (subap * self.yGrad).sum()

        # self.slopes[:self.activeSubaps] = self.xSlopes
        # self.slopes[self.activeSubaps:] = self.ySlopes

        # Remove tip-tilt if required
        if self.wfsConfig.removeTT == True:
            self.slopes[:self.activeSubaps] -= self.slopes[:self.activeSubaps].mean()
            self.slopes[self.activeSubaps:] -= self.slopes[self.activeSubaps:].mean()

        # Add 'angle equivalent noise' if asked for
        if self.wfsConfig.angleEquivNoise and not self.iMat:
            pxlEquivNoise = (
                    self.wfsConfig.angleEquivNoise *
                    float(self.wfsConfig.pxlsPerSubap)
                    /self.wfsConfig.subapFOV )
            self.slopes += numpy.random.normal( 0, pxlEquivNoise,
                                                2*self.activeSubaps)

        return self.slopes
#  ______                          _     _
#  | ___ \                        (_)   | |
#  | |_/ /   _ _ __ __ _ _ __ ___  _  __| |
#  |  __/ | | | '__/ _` | '_ ` _ \| |/ _` |
#  | |  | |_| | | | (_| | | | | | | | (_| |
#  \_|   \__, |_|  \__,_|_| |_| |_|_|\__,_|
#         __/ |
#        |___/

class Pyramid(WFS):
    """
    *Experimental* Pyramid WFS.

    This is an early prototype for a Pyramid WFS. Currently, its at a very early stage. It doesn't oscillate, so performance aint too good at the minute.

    To use, set the wfs parameter ``type'' to ``Pyramid'' type is a list of length number of wfs.
    """
    # oversampling for the first FFT from EField to focus (4 seems ok...)
    FOV_OVERSAMP = 4

    def calcInitParams(self):
        super(Pyramid, self).calcInitParams()
        self.FOVrad = self.wfsConfig.subapFOV * numpy.pi / (180. * 3600)

        self.FOVPxlNo = numpy.round(self.telDiam *
                                    self.FOVrad/self.wfsConfig.wavelength)

        self.detectorPxls = 2*self.wfsConfig.pxlsPerSubap
        self.scaledMask = aoSimLib.zoom(self.mask, self.FOVPxlNo)

        self.activeSubaps = self.wfsConfig.pxlsPerSubap**2

        while (self.wfsConfig.pxlsPerSubap*self.wfsConfig.fftOversamp
                    < self.FOVPxlNo):
            self.wfsConfig.fftOversamp += 1

    def initFFTs(self):

        self.FFT = AOFFT.FFT(   [self.FOV_OVERSAMP*self.FOVPxlNo,]*2,
                                axes=(0,1), mode="pyfftw",
                                fftw_FLAGS=("FFTW_DESTROY_INPUT",
                                            self.wfsConfig.fftwFlag),
                                THREADS=self.wfsConfig.fftwThreads
                                )

        self.iFFTPadding = self.FOV_OVERSAMP*(self.wfsConfig.fftOversamp*
                                            self.wfsConfig.pxlsPerSubap)
        self.iFFT = AOFFT.FFT(
                    [4, self.iFFTPadding, self.iFFTPadding],
                    axes=(1,2), mode="pyfftw",
                    THREADS = self.wfsConfig.fftwThreads,
                    fftw_FLAGS=("FFTW_DESTROY_INPUT", self.wfsConfig.fftwFlag),
                    direction="BACKWARD"
                    )

    def allocDataArrays(self):

        super(Pyramid, self).allocDataArrays()
        # Allocate arrays
        # Find sizes of detector planes

        self.paddedDetectorPxls = (2*self.wfsConfig.pxlsPerSubap
                                    *self.wfsConfig.fftOversamp)
        self.paddedDetectorPlane = numpy.zeros([self.paddedDetectorPxls]*2,
                                                dtype=DTYPE)

        self.focalPlane = numpy.zeros( [self.FOV_OVERSAMP*self.FOVPxlNo,]*2,
                                        dtype=CDTYPE)

        self.quads = numpy.zeros(
                    (4,self.focalPlane.shape[0]/2.,self.focalPlane.shape[1]/2.),
                    dtype=CDTYPE)

        self.wfsDetectorPlane = numpy.zeros([self.detectorPxls]*2,
                                            dtype=DTYPE)

        self.slopes = numpy.zeros(2*self.activeSubaps)

    def zeroData(self, detector=True, inter=True):
        """
        Sets data structures in WFS to zero.

        Parameters:
            detector (bool, optional): Zero the detector? default:True
            inter (bool, optional): Zero intermediate arrays? default:True
        """

        self.zeroPhaseData()

        if inter:
            self.paddedDetectorPlane[:] = 0

        if detector:
            self.wfsDetectorPlane[:] = 0

    def calcFocalPlane(self):
        '''
        takes the calculated pupil phase, and uses FFT
        to transform to the focal plane, and scales for correct FOV.
        '''
        # Apply tilt fix and scale EField for correct FOV
        self.pupilEField = self.EField[
                self.simConfig.simPad:-self.simConfig.simPad,
                self.simConfig.simPad:-self.simConfig.simPad
                ]
        self.pupilEField *= numpy.exp(1j*self.tiltFix)
        self.scaledEField = aoSimLib.zoom(
                self.pupilEField, self.FOVPxlNo)*self.scaledMask

        # Go to the focus
        self.FFT.inputData[:] = 0
        self.FFT.inputData[ :self.FOVPxlNo,
                            :self.FOVPxlNo ] = self.scaledEField
        self.focalPlane[:] = AOFFT.ftShift2d( self.FFT() )

        #Cut focus into 4
        shapeX, shapeY = self.focalPlane.shape
        n=0
        for x in xrange(2):
            for y in xrange(2):
                self.quads[n] = self.focalPlane[x*shapeX/2 : (x+1)*shapeX/2,
                                                y*shapeX/2 : (y+1)*shapeX/2]
                n+=1

        #Propogate each quadrant back to the pupil plane
        self.iFFT.inputData[:] = 0
        self.iFFT.inputData[:,
                            :0.5*self.FOV_OVERSAMP*self.FOVPxlNo,
                            :0.5*self.FOV_OVERSAMP*self.FOVPxlNo] = self.quads
        self.pupilImages = abs(AOFFT.ftShift2d(self.iFFT()))**2

        size = self.paddedDetectorPxls/2
        pSize = self.iFFTPadding/2.


        #add this onto the padded detector array
        for x in range(2):
            for y in range(2):
                self.paddedDetectorPlane[
                        x*size:(x+1)*size,
                        y*size:(y+1)*size] += self.pupilImages[
                                                2*x+y,
                                                pSize:pSize+size,
                                                pSize:pSize+size]

    def makeDetectorPlane(self):

        #Bin down to requried pixels
        self.wfsDetectorPlane[:] += aoSimLib.binImgs(
                        self.paddedDetectorPlane,
                        self.wfsConfig.fftOversamp
                        )

    def calculateSlopes(self):

        xDiff = (self.wfsDetectorPlane[ :self.wfsConfig.pxlsPerSubap,:]-
                    self.wfsDetectorPlane[  self.wfsConfig.pxlsPerSubap:,:])
        xSlopes = (xDiff[:,:self.wfsConfig.pxlsPerSubap]
                    +xDiff[:,self.wfsConfig.pxlsPerSubap:])

        yDiff = (self.wfsDetectorPlane[:, :self.wfsConfig.pxlsPerSubap]-
                    self.wfsDetectorPlane[:, self.wfsConfig.pxlsPerSubap:])
        ySlopes = (yDiff[:self.wfsConfig.pxlsPerSubap, :]
                    +yDiff[self.wfsConfig.pxlsPerSubap:, :])


        self.slopes[:] = numpy.append(xSlopes.flatten(), ySlopes.flatten())

    #Tilt optimisation
    ################################
    def calcTiltCorrect(self):
        """
        Calculates the required tilt to add to avoid the PSF being centred on
        only 1 pixel
        """
        if not self.wfsConfig.pxlsPerSubap%2:
            #Angle we need to correct
            theta = self.FOVrad/ (2*self.FOV_OVERSAMP*self.FOVPxlNo)

            A = theta*self.telDiam/(2*self.wfsConfig.wavelength)*2*numpy.pi

            coords = numpy.linspace(-1,1,self.simConfig.pupilSize)
            X,Y = numpy.meshgrid(coords,coords)

            self.tiltFix = -1*A*(X+Y)

        else:
            self.tiltFix = numpy.zeros((self.simConfig.pupilSize,)*2)
