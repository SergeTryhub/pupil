'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2017  Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

import os
import cv2
import numpy as np
from file_methods import save_object,load_object
from gl_utils import adjust_gl_view,clear_gl_screen,basic_gl_setup,make_coord_system_pixel_based,make_coord_system_norm_based
from methods import normalize


import OpenGL.GL as gl
from os import stat
from pyglui import ui
from pyglui.cygl.utils import draw_polyline,draw_points,RGBA,draw_gl_texture
from pyglui.pyfontstash import fontstash
from pyglui.ui import get_opensans_font_path
from glfw import *

from . calibration_plugin_base import Calibration_Plugin

#logging
import logging
logger = logging.getLogger(__name__)


#these are calibration we recorded. They are estimates and generalize our setup. Its always better to calibrate each camera.
pre_recorded_calibrations = {
                            'Pupil Cam1 ID2':{
                                (1280, 720):{
                                'dist_coefs': [[-0.14688911927649967], [-0.11345479375084214], [0.1881071629376831], [-0.10071646750776073]],
                                'camera_name': 'Pupil Cam1 ID2',
                                'resolution': (1280, 720),
                                'camera_matrix': [[848.7613106866924, 0.0, 650.4202362234881], [0.0, 817.2486157816005, 349.62847947455015], [0.0, 0.0, 1.0]]
                                    }
                                },
                            'Logitech Webcam C930e':{
                                (1280, 720):{
                                    'dist_coefs': [[0.3828165020558424], [0.32332222569113217], [-1.1181997066586369], [0.9835933020958403]],
                                    'camera_name': 'Logitech Webcam C930e',
                                    'resolution': (1280, 720),
                                    'camera_matrix': [[760.4092598632442, 0.0, 643.1354671418516], [0.0, 732.6537504697109, 374.91086611596864], [0.0, 0.0, 1.0]]
                                    }
                                },
                            }

def idealized_camera_calibration(resolution,f=1000.):
    return {   'dist_coefs': [[ 1./3.,2./15.,17./315.,62./2835.]], # For fisheye distortion this yields the identity function
               'camera_name': 'ideal camera with focal length {}'.format(f),
               'resolution': resolution,
               'camera_matrix': [[  f,     0., resolution[0]/2.],
                                 [    0.,  f,  resolution[1]/2.],
                                 [    0.,     0.,    1.  ]]
           }


def load_camera_calibration(g_pool):
    if g_pool.app == 'capture':
        try:
            camera_calibration = load_object(os.path.join(g_pool.user_dir,'camera_calibration'))
            camera_calibration['camera_name']
        except KeyError:
            camera_calibration = None
            logger.warning('Invalid or Deprecated camera calibration found. Please recalibrate camera.')
        except:
            camera_calibration = None
        else:
            same_name = camera_calibration['camera_name'] == g_pool.capture.name
            same_resolution = tuple(camera_calibration['resolution']) == g_pool.capture.frame_size
            if not (same_name and same_resolution):
                logger.warning('Loaded camera calibration but camera name and/or resolution has changed.')
                camera_calibration = None
            else:
                logger.info("Loaded user calibrated calibration for {}@{}.".format(g_pool.capture.name,g_pool.capture.frame_size))

        if not camera_calibration:
            logger.debug("Trying to load pre recorded calibration.")
            try:
                camera_calibration = pre_recorded_calibrations[g_pool.capture.name][g_pool.capture.frame_size]
            except KeyError:
                logger.info("Pre recorded calibration for {}@{} not found.".format(g_pool.capture.name,g_pool.capture.frame_size))
            else:
                logger.info("Loaded pre recorded calibration for {}@{}.".format(g_pool.capture.name,g_pool.capture.frame_size))


        if not camera_calibration:
            camera_calibration = idealized_camera_calibration(g_pool.capture.frame_size)
            logger.warning("Camera calibration not found. Will assume idealized camera. Please calibrate your cameras. Using camera 'Camera_Intrinsics_Estimation'.")

    else:
        try:
            camera_calibration = load_object(os.path.join(g_pool.rec_dir,'camera_calibration'))
        except:
            camera_calibration = idealized_camera_calibration(g_pool.capture.frame_size)
            logger.warning("Camera calibration not found. Will assume idealized camera. Please calibrate your cameras before your next recording.")
        else:
            logger.info("Loaded Camera calibration from file.")
    return camera_calibration


# window calbacks
def on_resize(window,w, h):
    active_window = glfwGetCurrentContext()
    glfwMakeContextCurrent(window)
    adjust_gl_view(w,h)
    glfwMakeContextCurrent(active_window)

class Camera_Intrinsics_Estimation_Fisheye(Calibration_Plugin):
    """Camera_Intrinsics_Calibration
        This method is not a gaze calibration.
        This method is used to calculate camera intrinsics.
    """
    def __init__(self,g_pool,fullscreen = False):
        super().__init__(g_pool)
        self.collect_new = False
        self.calculated = False
        self.obj_grid = _gen_pattern_grid((4, 11))
        self.img_points = []
        self.obj_points = []
        self.count = 10
        self.display_grid = _make_grid()

        self._window = None

        self.menu = None
        self.button = None
        self.clicks_to_close = 5
        self.window_should_close = False
        self.fullscreen = fullscreen
        self.monitor_idx = 0


        self.glfont = fontstash.Context()
        self.glfont.add_font('opensans',get_opensans_font_path())
        self.glfont.set_size(32)
        self.glfont.set_color_float((0.2,0.5,0.9,1.0))
        self.glfont.set_align_string(v_align='center')



        self.undist_img = None
        self.show_undistortion = False
        self.show_undistortion_switch = None


        self.camera_calibration = load_camera_calibration(self.g_pool)
        if self.camera_calibration:
            logger.info('Loaded camera calibration. Click show undistortion to verify.')
            logger.info('Hint: Straight lines in the real world should be straigt in the image.')
            self.camera_intrinsics = self.camera_calibration['camera_matrix'],self.camera_calibration['dist_coefs'],self.camera_calibration['resolution']
        else:
            self.camera_intrinsics = None

    def init_gui(self):

        monitor_names = [glfwGetMonitorName(m) for m in glfwGetMonitors()]
        #primary_monitor = glfwGetPrimaryMonitor()
        self.info = ui.Info_Text("Estimate Camera intrinsics of the world camera. Using an 11x9 asymmetrical circle grid. Click 'C' to capture a pattern.")
        self.g_pool.calibration_menu.append(self.info)

        self.menu = ui.Growing_Menu('Controls')
        self.menu.append(ui.Button('show Pattern',self.open_window))
        self.menu.append(ui.Selector('monitor_idx',self,selection = range(len(monitor_names)),labels=monitor_names,label='Monitor'))
        self.menu.append(ui.Switch('fullscreen',self,label='Use Fullscreen'))
        self.show_undistortion_switch = ui.Switch('show_undistortion',self,label='show undistorted image')
        self.menu.append(self.show_undistortion_switch)
        if not self.camera_intrinsics:
            self.show_undistortion_switch.read_only=True
        self.g_pool.calibration_menu.append(self.menu)

        self.button = ui.Thumb('collect_new',self,setter=self.advance,label='C',hotkey='c')
        self.button.on_color[:] = (.3,.2,1.,.9)
        self.g_pool.quickbar.insert(0,self.button)

    def deinit_gui(self):
        if self.menu:
            self.g_pool.calibration_menu.remove(self.menu)
            self.g_pool.calibration_menu.remove(self.info)

            self.menu = None
        if self.button:
            self.g_pool.quickbar.remove(self.button)
            self.button = None

    def do_open(self):
        if not self._window:
            self.window_should_open = True

    def get_count(self):
        return self.count

    def advance(self,_):
        if self.count == 10:
            logger.info("Capture 10 calibration patterns.")
            self.button.status_text = "{:d} to go".format(self.count)
            self.calculated = False
            self.img_points = []
            self.obj_points = []


        self.collect_new = True

    def open_window(self):
        if not self._window:
            if self.fullscreen:
                monitor = glfwGetMonitors()[self.monitor_idx]
                mode = glfwGetVideoMode(monitor)
                height,width= mode[0],mode[1]
            else:
                monitor = None
                height,width = 640,480

            self._window = glfwCreateWindow(height, width, "Calibration", monitor=monitor, share=glfwGetCurrentContext())
            if not self.fullscreen:
                # move to y = 31 for windows os
                glfwSetWindowPos(self._window,200,31)


            #Register callbacks
            glfwSetFramebufferSizeCallback(self._window,on_resize)
            glfwSetKeyCallback(self._window,self.on_key)
            glfwSetWindowCloseCallback(self._window,self.on_close)
            glfwSetMouseButtonCallback(self._window,self.on_button)

            on_resize(self._window,*glfwGetFramebufferSize(self._window))


            # gl_state settings
            active_window = glfwGetCurrentContext()
            glfwMakeContextCurrent(self._window)
            basic_gl_setup()
            glfwMakeContextCurrent(active_window)

            self.clicks_to_close = 5

    def on_key(self,window, key, scancode, action, mods):
        if action == GLFW_PRESS:
            if key == GLFW_KEY_ESCAPE:
                self.on_close()

    def on_button(self,window,button, action, mods):
        if action ==GLFW_PRESS:
            self.clicks_to_close -=1
        if self.clicks_to_close ==0:
            self.on_close()

    def on_close(self,window=None):
        self.window_should_close = True

    def close_window(self):
        self.window_should_close=False
        if self._window:
            glfwDestroyWindow(self._window)
            self._window = None

    def calculate(self):
        self.calculated = True
        self.count = 10

        calibration_flags = cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC + cv2.fisheye.CALIB_CHECK_COND + cv2.fisheye.CALIB_FIX_SKEW
        max_iter=30
        eps=1e-6
        img_shape = self.g_pool.capture.frame_size
        camera_matrix = np.zeros((3, 3))
        dist_coefs = np.zeros((4, 1))
        rvecs = [np.zeros((1, 1, 3), dtype=np.float64) for i in range(self.count)]
        tvecs = [np.zeros((1, 1, 3), dtype=np.float64) for i in range(self.count)]
        objPoints = [x.reshape(1,-1,3) for x in self.obj_points]
        imgPoints = self.img_points
        rms, _, _, _, _ = \
            cv2.fisheye.calibrate(
                objPoints,
                imgPoints,
                img_shape,
                camera_matrix,
                dist_coefs,
                rvecs,
                tvecs,
                calibration_flags,
                (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, max_iter, eps)
            )

        logger.info("Calibrated Camera, RMS:{}".format(rms))
        camera_calibration = {'camera_matrix':camera_matrix.tolist(),'dist_coefs':dist_coefs.tolist(),'camera_name':self.g_pool.capture.name,'resolution':self.g_pool.capture.frame_size}
        save_object(camera_calibration,os.path.join(self.g_pool.user_dir,"camera_calibration"))
        logger.info("Calibration saved to user folder")
        self.camera_intrinsics = camera_matrix.tolist(),dist_coefs.tolist(),self.g_pool.capture.frame_size
        self.show_undistortion_switch.read_only=False

    def update(self,frame,events):
        if self.collect_new:
            img = frame.img
            status, grid_points = cv2.findCirclesGrid(img, (4,11), flags=cv2.CALIB_CB_ASYMMETRIC_GRID)
            if status:
                self.img_points.append(grid_points)
                self.obj_points.append(self.obj_grid)
                self.collect_new = False
                self.count -=1
                self.button.status_text = "{:d} to go".format(self.count)


        if self.count<=0 and not self.calculated:
            self.calculate()
            self.button.status_text = ''

        if self.window_should_close:
            self.close_window()

        if self.show_undistortion:
            # This function is not yet compatible with the fisheye camera model and would have to be manually implemented.
            # adjusted_k,roi = cv2.getOptimalNewCameraMatrix(cameraMatrix= np.array(self.camera_intrinsics[0]), distCoeffs=np.array(self.camera_intrinsics[1]), imageSize=self.camera_intrinsics[2], alpha=0.5,newImgSize=self.camera_intrinsics[2],centerPrincipalPoint=1)
            self.undist_img = self.undistort(frame.img, self.camera_intrinsics[0], self.camera_intrinsics[1], self.g_pool.capture.frame_size)

    @staticmethod
    def undistort(img, K, D, target_size):
        R = np.eye(3)

        map1, map2 = cv2.fisheye.initUndistortRectifyMap(
            np.array(K),
            np.array(D),
            R,
            np.array(K),
            target_size,
            cv2.CV_16SC2
        )

        undistorted_img = cv2.remap(
            img,
            map1,
            map2,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT
        )

        return undistorted_img

    @staticmethod
    def projectPoints(object_points, K, D, skew=0, rvec=None, tvec=None):

        if object_points.ndim ==3:
            object_points = object_points.reshape(-1,3)

        if object_points.ndim == 2:
            object_points = np.expand_dims(object_points, 0)

        if rvec is None:
            rvec = np.zeros(3).reshape(1, 1, 3)
        else:
            rvec = np.array(rvec).reshape(1, 1, 3)

        if tvec is None:
            tvec = np.zeros(3).reshape(1, 1, 3)
        else:
            tvec = np.array(tvec).reshape(1, 1, 3)

        image_points, jacobian = cv2.fisheye.projectPoints(
            object_points,
            rvec,
            tvec,
            K,
            D,
            alpha=skew
        )

        image_points = np.squeeze(image_points)
        image_points = np.expand_dims(image_points, 1)
        return image_points

    @staticmethod
    def undistortPoints(distorted, K, D, R=np.eye(3)):
        """Undistorts 2D points using fisheye model.
        """

        if distorted.ndim ==3:
            distorted = distorted.reshape(-1,2)

        if distorted.ndim == 2:
            distorted = np.expand_dims(distorted, 0)

        undistorted = cv2.fisheye.undistortPoints(
            distorted.astype(np.float32),
            K,
            D,
            R=R,
            P=K
        )

        undistorted = np.squeeze(undistorted)
        undistorted = np.expand_dims(undistorted, 1)
        return undistorted
    @staticmethod
    def solvePnP(uv3d, xy, K, D):
        xy_undist = Camera_Intrinsics_Estimation_Fisheye.undistortPoints(xy, K, D)
        res = cv2.solvePnP(uv3d, xy_undist, K, np.array([[0,0,0,0,0]]), flags=cv2.SOLVEPNP_ITERATIVE)
        return res

    def gl_display(self):

        for grid_points in self.img_points:
            calib_bounds =  cv2.convexHull(grid_points)[:,0] #we dont need that extra encapsulation that opencv likes so much
            draw_polyline(calib_bounds,1,RGBA(0.,0.,1.,.5),line_type=gl.GL_LINE_LOOP)

        if self._window:
            self.gl_display_in_window()

        if self.show_undistortion:
            gl.glPushMatrix()
            make_coord_system_norm_based()
            draw_gl_texture(self.undist_img)
            gl.glPopMatrix()
    def gl_display_in_window(self):
        active_window = glfwGetCurrentContext()
        glfwMakeContextCurrent(self._window)

        clear_gl_screen()

        gl.glMatrixMode(gl.GL_PROJECTION)
        gl.glLoadIdentity()
        p_window_size = glfwGetWindowSize(self._window)
        r = p_window_size[0]/15.
        # compensate for radius of marker
        gl.glOrtho(-r,p_window_size[0]+r,p_window_size[1]+r,-r ,-1,1)
        # Switch back to Model View Matrix
        gl.glMatrixMode(gl.GL_MODELVIEW)
        gl.glLoadIdentity()
        #hacky way of scaling and fitting in different window rations/sizes
        grid = _make_grid()*min((p_window_size[0],p_window_size[1]*5.5/4.))
        #center the pattern
        grid -= np.mean(grid)
        grid +=(p_window_size[0]/2-r,p_window_size[1]/2+r)

        draw_points(grid,size=r,color=RGBA(0.,0.,0.,1),sharpness=0.95)

        if self.clicks_to_close <5:
            self.glfont.set_size(int(p_window_size[0]/30.))
            self.glfont.draw_text(p_window_size[0]/2.,p_window_size[1]/4.,'Touch {} more times to close window.'.format(self.clicks_to_close))

        glfwSwapBuffers(self._window)
        glfwMakeContextCurrent(active_window)


    def get_init_dict(self):
        return {}


    def cleanup(self):
        """gets called when the plugin get terminated.
        This happens either voluntarily or forced.
        if you have a gui or glfw window destroy it here.
        """
        if self._window:
            self.close_window()
        self.deinit_gui()


def _gen_pattern_grid(size=(4,11)):
    pattern_grid = []
    for i in range(size[1]):
        for j in range(size[0]):
            pattern_grid.append([(2*j)+i%2,i,0])
    return np.asarray(pattern_grid, dtype='f4')


def _make_grid(dim=(11,4)):
    """
    this function generates the structure for an asymmetrical circle grid
    domain (0-1)
    """
    x,y = range(dim[0]),range(dim[1])
    p = np.array([[[s,i] for s in x] for i in y], dtype=np.float32)
    p[:,1::2,1] += 0.5
    p = np.reshape(p, (-1,2), 'F')

    # scale height = 1
    x_scale =  1./(np.amax(p[:,0])-np.amin(p[:,0]))
    y_scale =  1./(np.amax(p[:,1])-np.amin(p[:,1]))

    p *=x_scale,x_scale/.5

    return p


