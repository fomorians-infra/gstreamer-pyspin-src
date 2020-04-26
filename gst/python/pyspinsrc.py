import enum
from dataclasses import dataclass
import math
from typing import Any, List, Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
gi.require_version("GObject", "2.0")
gi.require_version("GstVideo", "1.0")

from gi.repository import Gst, GObject, GLib, GstBase, GstVideo

from gstreamer.utils import gst_buffer_with_pad_to_ndarray

try:
    import numpy as np
except ImportError:
    Gst.error("pyspinsrc requires numpy")
    raise

try:
    import PySpin
except ImportError:
    Gst.error("pyspinsrc requires PySpin")
    raise


class ImageAcquirer:

    TIMEOUT_MS = 2000

    def __init__(self):
        self._system = PySpin.System.GetInstance()
        self._device_list = self._system.GetCameras()
        self._current_device = None
        self._device_node_map = None
        self._tl_device_node_map = None
        self._tl_stream_node_map = None

    def __del__(self):
        self._reset_cam()
        self._device_list.Clear()
        self._system.ReleaseInstance()

    def update_device_list(self):
        self._device_list = self._system.GetCameras()

    def get_device_count(self, update_list: bool = True) -> int:
        if update_list:
            self.update_device_list()
        return self._device_list.GetSize()

    def init_device(self, device_serial: str = None, device_index: int = None,) -> bool:
        # reset cam
        self._reset_cam()

        self.update_device_list()

        candidate_devices: List[PySpin.Camera] = []

        if device_serial is not None:
            candidate_devices.append(self._device_list.GetBySerial(device_serial))

        if device_index is not None and device_index < self.get_device_count(
            update_list=False
        ):
            candidate_devices.append(self._device_list.GetByIndex(device_index))

        self._current_device = next(
            (dev for dev in candidate_devices if dev and dev.IsValid()), None
        )

        if self._current_device is None:
            raise ValueError(
                f"No device with serial number '{device_serial}' or index '{device_index}' is available"
            )

        self._current_device.Init()

        # Ensure that acquisition is stopped
        try:
            self.end_acquisition()
        except ValueError:
            pass

        return True

    def _reset_cam(self):
        if self._current_device is not None and self._current_device.IsValid():
            if self._current_device.IsStreaming():
                self._current_device.EndAcquisition()
            if self._current_device.IsInitialized():
                self._current_device.DeInit()

        self._device_node_map = None
        self._tl_device_node_map = None
        self._tl_stream_node_map = None

        del self._current_device
        self._current_device = None

    def start_acquisition(self):
        self.set_node_val("AcquisitionMode", "Continuous")
        try:
            self._current_device.BeginAcquisition()
        except PySpin.SpinnakerException as ex:
            raise ValueError(f"Error: {ex}")

    def end_acquisition(self):
        try:
            self._current_device.EndAcquisition()
        except PySpin.SpinnakerException as ex:
            raise ValueError(f"Error: {ex}")

    def get_next_image(self, logger: Callable[[str], None] = None) -> (np.ndarray, int):
        spinnaker_image = None

        while spinnaker_image is None or spinnaker_image.IsIncomplete():

            spinnaker_image = self._current_device.GetNextImage(self.TIMEOUT_MS)

            if spinnaker_image.IsIncomplete():
                if logger:
                    logger(
                        f"Image incomplete with image status {spinnaker_image.GetImageStatus()}"
                    )
                spinnaker_image.Release()

        image_array = spinnaker_image.GetNDArray()

        if image_array.ndim == 2:
            image_array = np.expand_dims(image_array, axis=2)

        image_timestamp = spinnaker_image.GetTimeStamp()

        spinnaker_image.Release()

        return (image_array, image_timestamp)

    def _get_device_node_map(self) -> PySpin.NodeMap:
        if (
            self._current_device is None
            or not self._current_device.IsValid()
            or not self._current_device.IsInitialized()
        ):
            raise ValueError("No device has been selected and initialied")
        if self._device_node_map is None:
            self._device_node_map = self._current_device.GetNodeMap()
        return self._device_node_map

    def _get_tl_device_node_map(self) -> PySpin.NodeMap:
        if self._current_device is None or not self._current_device.IsValid():
            raise ValueError("No device has been selected")
        if self._tl_device_node_map is None:
            self._tl_device_node_map = self._current_device.GetTLDeviceNodeMap()
        return self._tl_device_node_map

    def _get_tl_stream_node_map(self) -> PySpin.NodeMap:
        if self._current_device is None or not self._current_device.IsValid():
            raise ValueError("No device has been selected")
        if self._tl_stream_node_map is None:
            self._tl_stream_node_map = self._current_device.GetTLStreamNodeMap()
        return self._tl_stream_node_map

    def _get_device_id(self) -> str:
        if self._current_device is None or not self._current_device.IsValid():
            return None
        return self._current_device.TLDevice.DeviceSerialNumber.GetValue()

    def _get_node(self, node_name: str) -> PySpin.INodeMap:
        device_node = self._get_device_node_map().GetNode(node_name)
        if device_node is not None:
            return device_node

        tl_device_node = self._get_tl_device_node_map().GetNode(node_name)
        if tl_device_node is not None:
            return tl_device_node

        tl_stream_node = self._get_tl_stream_node_map().GetNode(node_name)
        if tl_stream_node is not None:
            return tl_stream_node

        return None

    def node_available(self, node_name: str) -> bool:
        node = self._get_node(node_name)
        return node is not None and PySpin.IsAvailable(node)

    def get_node_val(self, node_name: str) -> Any:
        node: PySpin.INode = self._get_node(node_name)
        if node is None:
            raise ValueError(f"{node_name} node is not available")
        elif "GetPrincipalInterfaceType" not in dir(node):
            raise ValueError(f"Could not determine the type of node: {node_name}")
        elif node.GetPrincipalInterfaceType() == PySpin.intfIInteger:
            return self._get_int_node_val(PySpin.CIntegerPtr(node))
        elif node.GetPrincipalInterfaceType() == PySpin.intfIFloat:
            return self._get_float_node_val(PySpin.CFloatPtr(node))
        elif node.GetPrincipalInterfaceType() == PySpin.intfIBoolean:
            return self._get_bool_node_val(PySpin.CBooleanPtr(node))
        elif node.GetPrincipalInterfaceType() == PySpin.intfIEnumeration:
            return self._get_enum_node_val(PySpin.CEnumerationPtr(node))
        elif node.GetPrincipalInterfaceType() == PySpin.intfIString:
            raise NotImplementedError("No getter implemented for string nodes")
        elif node.GetPrincipalInterfaceType() == PySpin.intfICommand:
            raise NotImplementedError("No getter implemented for command nodes")
        else:
            raise ValueError(
                f"{node_name} node is of unknown type: {node.GetPrincipalInterfaceType()}"
            )

    def set_node_val(self, node_name: str, value: Any):
        node: PySpin.INode = self._get_node(node_name)
        if node is None:
            raise ValueError(f"{node_name} node is not available")
        elif "GetPrincipalInterfaceType" not in dir(node):
            raise ValueError(f"Could not determine the type of node: {node_name}")
        elif node.GetPrincipalInterfaceType() == PySpin.intfIInteger:
            return self._set_int_node_val(PySpin.CIntegerPtr(node), value)
        elif node.GetPrincipalInterfaceType() == PySpin.intfIFloat:
            return self._set_float_node_val(PySpin.CFloatPtr(node), value)
        elif node.GetPrincipalInterfaceType() == PySpin.intfIBoolean:
            return self._set_bool_node_val(PySpin.CBooleanPtr(node), value)
        elif node.GetPrincipalInterfaceType() == PySpin.intfIEnumeration:
            return self._set_enum_node_val(PySpin.CEnumerationPtr(node), value)
        elif node.GetPrincipalInterfaceType() == PySpin.intfIString:
            raise NotImplementedError("No setter implemented for string nodes")
        elif node.GetPrincipalInterfaceType() == PySpin.intfICommand:
            raise NotImplementedError("No setter implemented for command nodes")
        else:
            raise ValueError(
                f"{node_name} node is of unknown type: {node.GetPrincipalInterfaceType()}"
            )

    def execute_node(self, node_name: str):
        node: PySpin.INode = self._get_node(node_name)
        if node is None:
            raise ValueError(f"{node_name} node is not available")
        elif "GetPrincipalInterfaceType" not in dir(node):
            raise ValueError(f"Could not determine the type of node: {node_name}")
        elif node.GetPrincipalInterfaceType() == PySpin.intfICommand:
            self._execute_command_node(node_name)
        else:
            raise ValueError(
                f"{node_name} node is of unknown type: {node.GetPrincipalInterfaceType()}"
            )

    def get_node_range(self, node_name: str) -> (Any, Any):
        node: PySpin.INode = self._get_node(node_name)
        if node is None:
            raise ValueError(f"{node_name} node is not available")
        elif "GetPrincipalInterfaceType" not in dir(node):
            raise ValueError(f"Could not determine the type of node: {node_name}")
        elif node.GetPrincipalInterfaceType() == PySpin.intfIInteger:
            return self._get_int_node_range(PySpin.CIntegerPtr(node))
        elif node.GetPrincipalInterfaceType() == PySpin.intfIFloat:
            return self._get_float_node_range(PySpin.CFloatPtr(node))
        else:
            raise ValueError(
                f"Range not available for {node_name} node of type: {node.GetPrincipalInterfaceType()}"
            )

    def get_node_entries(self, node_name: str) -> List[Any]:
        node: PySpin.INode = self._get_node(node_name)
        if node is None:
            raise ValueError(f"{node_name} node is not available")
        elif "GetPrincipalInterfaceType" not in dir(node):
            raise ValueError(f"Could not determine the type of node: {node_name}")
        elif node.GetPrincipalInterfaceType() == PySpin.intfIEnumeration:
            return self._get_available_enum_entries(PySpin.CEnumerationPtr(node))
        else:
            raise ValueError(
                f"Range not available for {node_name} node of type: {node.GetPrincipalInterfaceType()}"
            )

    def _get_int_node_val(self, int_node: PySpin.CIntegerPtr) -> int:
        if not PySpin.IsAvailable(int_node) or not PySpin.IsReadable(int_node):
            raise ValueError(
                f"Integer node '{int_node.GetDisplayName()}' is not readable"
            )
        return int_node.GetValue()

    def _get_int_node_range(self, int_node: PySpin.CIntegerPtr) -> (int, int):
        if not PySpin.IsAvailable(int_node) or not PySpin.IsReadable(int_node):
            raise ValueError(
                f"Integer node '{int_node.GetDisplayName()}' is not writable"
            )
        return (int_node.GetMin(), int_node.GetMax())

    def _set_int_node_val(self, int_node: PySpin.CIntegerPtr, value: int):
        if not PySpin.IsAvailable(int_node) or not PySpin.IsWritable(int_node):
            raise ValueError(
                f"Integer node '{int_node.GetDisplayName()}' is not writable"
            )

        value = max(value, int_node.GetMin())
        value = min(value, int_node.GetMax())

        int_node.SetValue(int(value))

    def _get_float_node_val(self, float_node: PySpin.CFloatPtr) -> (float, float):
        if not PySpin.IsAvailable(float_node) or not PySpin.IsReadable(float_node):
            raise ValueError(
                f"Float node '{float_node.GetDisplayName()}' is not readable"
            )
        return float_node.GetValue()

    def _get_float_node_range(self, float_node: PySpin.CFloatPtr) -> (int, int):
        if not PySpin.IsAvailable(float_node) or not PySpin.IsReadable(float_node):
            raise ValueError(
                f"Float node '{float_node.GetDisplayName()}' is not readable"
            )
        return (float_node.GetMin(), float_node.GetMax())

    def _set_float_node_val(self, float_node: PySpin.CFloatPtr, value: float):
        if not PySpin.IsAvailable(float_node) or not PySpin.IsWritable(float_node):
            raise ValueError(
                f"Float node '{float_node.GetDisplayName()}' is not writable"
            )

        value = max(value, float_node.GetMin())
        value = min(value, float_node.GetMax())
        float_node.SetValue(float(value))

    def _get_bool_node_val(self, bool_node: PySpin.CBooleanPtr) -> bool:
        if not PySpin.IsAvailable(bool_node) or not PySpin.IsReadable(bool_node):
            raise ValueError(
                f"Boolean node '{bool_node.GetDisplayName()}' is not readable"
            )
        return bool_node.GetValue()

    def _set_bool_node_val(self, bool_node: PySpin.CBooleanPtr, value: bool):
        if not PySpin.IsAvailable(bool_node) or not PySpin.IsWritable(bool_node):
            raise ValueError(
                f"Boolean node '{bool_node.GetDisplayName()}' is not writable"
            )

        bool_node.SetValue(bool(value))

    def _get_available_enum_entries(
        self, enum_node: PySpin.CEnumerationPtr
    ) -> List[str]:
        if not PySpin.IsAvailable(enum_node) or not PySpin.IsReadable(enum_node):
            raise ValueError(
                f"Enumeration node '{enum_node.GetDisplayName()}' is not readable"
            )

        available_entries = [
            PySpin.CEnumEntryPtr(pf).GetSymbolic()
            for pf in enum_node.GetEntries()
            if PySpin.IsAvailable(pf)
        ]
        return available_entries

    def _get_enum_node_val(self, enum_node: PySpin.CEnumerationPtr) -> str:
        if not PySpin.IsAvailable(enum_node) or not PySpin.IsReadable(enum_node):
            raise ValueError(
                f"Enumeration node '{enum_node.GetDisplayName()}' is not readable"
            )
        return enum_node.GetCurrentEntry().GetSymbolic()

    def _set_enum_node_val(self, enum_node: PySpin.CEnumerationPtr, value: str):
        if not PySpin.IsAvailable(enum_node) or not PySpin.IsWritable(enum_node):
            raise ValueError(
                f"Enumeration node '{enum_node.GetDisplayName()}' is not writable"
            )

        enum_entry = enum_node.GetEntryByName(str(value))
        if not PySpin.IsAvailable(enum_entry) or not PySpin.IsReadable(enum_entry):
            raise ValueError(
                f"Entry '{value}' for enumeration node '{enum_node.GetDisplayName()}' is not available"
            )

        enum_node.SetIntValue(enum_entry.GetValue())

    def _execute_command_node(self, node_name: str):
        command_node = PySpin.CCommandPtr(self._get_node(node_name))
        if not PySpin.IsAvailable(command_node) or not PySpin.IsWritable(command_node):
            raise ValueError(f"Error: Command node '{node_name}' is not writable")

        command_node.Execute()


class PySpinSrc(GstBase.PushSrc):

    GST_PLUGIN_NAME = "pyspinsrc"

    DEFAULT_AUTO_EXPOSURE = True
    DEFAULT_AUTO_GAIN = True
    DEFAULT_EXPOSURE_TIME = -1
    DEFAULT_GAIN = -1
    DEFAULT_AUTO_WB = True
    DEFAULT_WB_BLUE = -1
    DEFAULT_WB_RED = -1
    DEFAULT_H_BINNING = 1
    DEFAULT_V_BINNING = 1
    DEFAULT_OFFSET_X = 0
    DEFAULT_OFFSET_Y = 0
    DEFAULT_NUM_BUFFERS = 10
    DEFAULT_SERIAL_NUMBER = None
    DEFAULT_USER_SET = "Default"

    MILLISECONDS_PER_NANOSECOND = 1000000

    __gstmetadata__ = ("pyspinsrc", "Src", "PySpin src element", "Brian Ofrim")

    __gsttemplates__ = Gst.PadTemplate.new(
        "src", Gst.PadDirection.SRC, Gst.PadPresence.ALWAYS, Gst.Caps.new_any(),
    )

    __gproperties__ = {
        "auto-exposure": (
            bool,
            "automatic exposure timing",
            "Enable the automatic exposure time algorithm",
            DEFAULT_AUTO_EXPOSURE,
            GObject.ParamFlags.READWRITE,
        ),
        "auto-gain": (
            bool,
            "automatic gain",
            "Enable the automatic gain algorithm",
            DEFAULT_AUTO_GAIN,
            GObject.ParamFlags.READWRITE,
        ),
        "exposure": (
            float,
            "exposure time",
            "Exposure time in microsecods",
            -1,
            100000000.0,
            DEFAULT_EXPOSURE_TIME,
            GObject.ParamFlags.READWRITE,
        ),
        "gain": (
            float,
            "gain",
            "Gain in decibels",
            -1,
            100.0,
            DEFAULT_GAIN,
            GObject.ParamFlags.READWRITE,
        ),
        "auto-wb": (
            bool,
            "automatic white balance",
            "Enable the automatic white balance algorithm",
            DEFAULT_AUTO_WB,
            GObject.ParamFlags.READWRITE,
        ),
        "wb-blue-ratio": (
            float,
            "White balance blue ratio",
            "White balance blue/green ratio (If neither wb-blue or wb-red are specified, auto wb is used)",
            -1,
            10.0,
            DEFAULT_WB_BLUE,
            GObject.ParamFlags.READWRITE,
        ),
        "wb-red-ratio": (
            float,
            "White balance red ratio",
            "White balance red/green ratio (If neither wb-blue or wb-red are specified, auto wb is used)",
            -1,
            10.0,
            DEFAULT_WB_BLUE,
            GObject.ParamFlags.READWRITE,
        ),
        "h-binning": (
            int,
            "horizontal binning",
            "Horizontal average binning (applied before width and offset x)",
            1,
            GLib.MAXINT,
            DEFAULT_H_BINNING,
            GObject.ParamFlags.READWRITE,
        ),
        "v-binning": (
            int,
            "vertical binning",
            "Vertical average binning (applied before height and offset y)",
            1,
            GLib.MAXINT,
            DEFAULT_V_BINNING,
            GObject.ParamFlags.READWRITE,
        ),
        "offset-x": (
            int,
            "offset x",
            "Horizontal offset for the region of interest",
            0,
            GLib.MAXINT,
            DEFAULT_OFFSET_X,
            GObject.ParamFlags.READWRITE,
        ),
        "offset-y": (
            int,
            "offset y",
            "Vertical offset for the region of interest",
            0,
            GLib.MAXINT,
            DEFAULT_OFFSET_Y,
            GObject.ParamFlags.READWRITE,
        ),
        "num-image-buffers": (
            int,
            "number of image buffers",
            "Number of buffers for Spinnaker to allocate for buffer handling",
            1,
            GLib.MAXINT,
            DEFAULT_NUM_BUFFERS,
            GObject.ParamFlags.READWRITE,
        ),
        "serial": (
            str,
            "serial number",
            "The camera serial number",
            DEFAULT_SERIAL_NUMBER,
            GObject.ParamFlags.READWRITE,
        ),
        "user-set": (
            str,
            "user set",
            "User set to apply properties on top of",
            DEFAULT_USER_SET,
            GObject.ParamFlags.READWRITE,
        ),
    }

    @dataclass
    class PixelFormatType:
        cap_type: str
        gst: str
        genicam: str

    RAW_CAP_TYPE = "video/x-raw"
    BAYER_CAP_TYPE = "video/x-bayer"

    SUPPORTED_PIXEL_FORMATS = [
        PixelFormatType(cap_type=RAW_CAP_TYPE, gst="GRAY8", genicam="Mono8"),
        #    PixelFormatType(cap_type=RAW_CAP_TYPE, gst="GRAY16_LE", genicam="Mono16"),
        PixelFormatType(cap_type=RAW_CAP_TYPE, gst="UYVY", genicam="YUV422Packed"),
        PixelFormatType(cap_type=RAW_CAP_TYPE, gst="YUY2", genicam="YCbCr422_8"),
        PixelFormatType(cap_type=RAW_CAP_TYPE, gst="RGB", genicam="RGB8"),
        PixelFormatType(cap_type=RAW_CAP_TYPE, gst="BGR", genicam="BGR8"),
        PixelFormatType(cap_type=BAYER_CAP_TYPE, gst="rggb", genicam="BayerRG8"),
        PixelFormatType(cap_type=BAYER_CAP_TYPE, gst="gbrg", genicam="BayerGB8"),
        PixelFormatType(cap_type=BAYER_CAP_TYPE, gst="bggr", genicam="BayerBG8"),
        PixelFormatType(cap_type=BAYER_CAP_TYPE, gst="grbg", genicam="BayerGR8"),
    ]

    # GST function
    def __init__(self):
        super(PySpinSrc, self).__init__()

        # Initialize properties before Base Class initialization
        self.info = GstVideo.VideoInfo()

        # Properties
        self.auto_exposure: bool = self.DEFAULT_AUTO_EXPOSURE
        self.auto_gain: bool = self.DEFAULT_AUTO_GAIN
        self.exposure_time: float = self.DEFAULT_EXPOSURE_TIME
        self.gain: float = self.DEFAULT_GAIN
        self.auto_wb: bool = self.DEFAULT_AUTO_WB
        self.wb_blue: float = self.DEFAULT_WB_BLUE
        self.wb_red: float = self.DEFAULT_WB_RED
        self.h_binning: int = self.DEFAULT_H_BINNING
        self.v_binning: int = self.DEFAULT_V_BINNING
        self.offset_x: int = self.DEFAULT_OFFSET_X
        self.offset_y: int = self.DEFAULT_OFFSET_Y
        self.num_cam_buffers: int = self.DEFAULT_NUM_BUFFERS
        self.serial: str = self.DEFAULT_SERIAL_NUMBER
        self.user_set: str = self.DEFAULT_USER_SET

        # Camera capabilities
        self.camera_caps = None

        # Image Capture Device
        self.image_acquirer: ImageAcquirer = None

        # Buffer timing
        self.timestamp_offset: int = 0
        self.previous_timestamp: int = 0

        # Base class properties
        self.set_live(True)
        self.set_format(Gst.Format.TIME)

    # GST function
    def do_get_property(self, prop: GObject.GParamSpec):
        if prop.name == "auto-exposure":
            return self.auto_exposure
        elif prop.name == "auto-gain":
            return self.auto_gain
        elif prop.name == "exposure":
            return self.exposure_time
        elif prop.name == "gain":
            return self.gain
        elif prop.name == "auto-wb":
            return self.auto_wb
        elif prop.name == "wb-red-ratio":
            return self.wb_red
        elif prop.name == "h-binning":
            return self.h_binning
        elif prop.name == "v-binning":
            return self.v_binning
        elif prop.name == "offset-x":
            return self.offset_x
        elif prop.name == "offset-y":
            return self.offset_y
        elif prop.name == "num-image-buffers":
            return self.num_cam_buffers
        elif prop.name == "serial":
            return self.serial
        elif prop.name == "user-set":
            return self.user_set
        else:
            raise AttributeError("unknown property %s" % prop.name)

    # GST function
    def do_set_property(self, prop: GObject.GParamSpec, value):
        Gst.info(f"Setting {prop.name} = {value}")
        if prop.name == "auto-exposure":
            self.auto_exposure = value
        elif prop.name == "auto-gain":
            self.auto_gain = value
        elif prop.name == "exposure":
            self.exposure_time = value
        elif prop.name == "gain":
            self.gain = value
        elif prop.name == "auto-wb":
            self.auto_wb = value
        elif prop.name == "wb-blue-ratio":
            self.wb_blue = value
        elif prop.name == "wb-red-ratio":
            self.wb_red = value
        elif prop.name == "h-binning":
            self.h_binning = value
        elif prop.name == "v-binning":
            self.v_binning = value
        elif prop.name == "offset-x":
            self.offset_x = value
        elif prop.name == "offset-y":
            self.offset_y = value
        elif prop.name == "num-image-buffers":
            self.num_cam_buffers = value
        elif prop.name == "serial":
            self.serial = value
        elif prop.name == "user-set":
            self.user_set = value
        else:
            raise AttributeError("unknown property %s" % prop.name)

    # helper function
    def get_format_from_genicam(self, genicam_format: str) -> "PixelFormatType":
        return next(
            (
                f
                for f in self.SUPPORTED_PIXEL_FORMATS
                if f.genicam.lower() == genicam_format.lower()
            ),
            None,
        )

    # helper function
    def get_format_from_gst(self, gst_format: str) -> "PixelFormatType":
        return next(
            (
                f
                for f in self.SUPPORTED_PIXEL_FORMATS
                if f.gst.lower() == gst_format.lower()
            ),
            None,
        )

    # helper function
    def apply_caps_to_cam(self) -> bool:
        Gst.info("Applying caps.")
        try:

            genicam_format = self.get_format_from_gst(self.info.finfo.name).genicam
            self.set_cam_node_val("PixelFormat", genicam_format)

            self.set_cam_node_val("Height", self.info.height)
            self.set_cam_node_val("Width", self.info.width)
            self.set_cam_node_val("OffsetY", self.offset_y)
            self.set_cam_node_val("OffsetX", self.offset_x)

            if self.cam_node_available("AcquisitionFrameRateEnable"):
                self.set_cam_node_val("AcquisitionFrameRateEnable", True)
            else:
                self.set_cam_node_val("AcquisitionFrameRateAuto", "Off")
                self.set_cam_node_val("AcquisitionFrameRateEnabled", True)

            self.set_cam_node_val(
                "AcquisitionFrameRate", self.info.fps_n / self.info.fps_d
            )

        except (ValueError, NotImplementedError) as ex:
            Gst.error(f"Error: {ex}")
            return False
        return True

    # helper function
    def cam_node_available(self, node_name: str) -> bool:
        try:
            return self.image_acquirer.node_available(node_name)
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")
            return False

    # helper function
    def get_cam_node_val(self, node_name: str) -> Any:
        try:
            return self.image_acquirer.get_node_val(node_name)
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")
            return None

    # helper function
    def set_cam_node_val(self, node_name: str, value, log_value: bool = True):
        try:
            self.image_acquirer.set_node_val(node_name, value)
            if log_value:
                Gst.info(f"{node_name}: {self.image_acquirer.get_node_val(node_name)}")
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")

    # helper function
    def execute_cam_node(self, node_name: str, log_execution: bool = True):
        try:
            self.image_acquirer.execute_node(node_name)
            if log_execution:
                Gst.info(f"{node_name} executed")
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")

    # helper function
    def get_cam_node_entries(self, node_name: str) -> List[Any]:
        try:
            return self.image_acquirer.get_node_entries(node_name)
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")
            return []

    # helper function
    def get_cam_node_range(self, node_name: str) -> (Any, Any):
        try:
            return self.image_acquirer.get_node_range(node_name)
        except (ValueError, NotImplementedError) as ex:
            Gst.warning(f"Warning: {ex}")
            return (None, None)

    # helper function
    def apply_properties_to_cam(self) -> bool:
        Gst.info("Applying properties")
        try:
            self.set_cam_node_val("UserSetSelector", self.user_set)
            self.execute_cam_node("UserSetLoad")

            self.set_cam_node_val("StreamBufferHandlingMode", "OldestFirst")
            self.set_cam_node_val("StreamBufferCountMode", "Manual")
            self.set_cam_node_val("StreamBufferCountManual", self.num_cam_buffers)

            # Configure Camera Properties
            if self.h_binning > 1:
                self.set_cam_node_val("BinningHorizontal", self.h_binning)

            if self.v_binning > 1:
                self.set_cam_node_val("BinningVertical", self.v_binning)

            if self.exposure_time >= 0:
                self.set_cam_node_val("ExposureAuto", "Off")
                self.set_cam_node_val("ExposureTime", self.exposure_time)

            if self.auto_exposure:
                self.set_cam_node_val("ExposureAuto", "Continuous")

            if self.gain >= 0:
                self.set_cam_node_val("GainAuto", "Off")
                self.set_cam_node_val("Gain", self.gain)

            if self.auto_gain:
                self.set_cam_node_val("GainAuto", "Continuous")

            if self.cam_node_available("BalanceWhiteAuto"):
                if self.wb_blue >= 0:
                    self.set_cam_node_val("BalanceWhiteAuto", "Off")
                    self.set_cam_node_val("BalanceRatioSelector", "Blue")
                    self.set_cam_node_val("BalanceRatio", self.wb_blue)

                if self.wb_red >= 0:
                    self.set_cam_node_val("BalanceWhiteAuto", "Off", Gst.info)
                    self.set_cam_node_val("BalanceRatioSelector", "Red", Gst.info)
                    self.set_cam_node_val("BalanceRatio", self.wb_red, Gst.info)

                if self.auto_wb:
                    self.set_cam_node_val("BalanceWhiteAuto", "Continuous")

        except Exception as ex:
            Gst.error(f"Error: {ex}")
            return False

        return True

    # helper function
    def get_camera_caps(self) -> Gst.Caps:

        # Get current pixel format
        starting_pixel_format = self.get_cam_node_val("PixelFormat")

        genicam_formats = self.get_cam_node_entries("PixelFormat")

        supported_pixel_formats = [
            self.get_format_from_genicam(pf) for pf in genicam_formats
        ]

        supported_pixel_formats = [
            pf for pf in supported_pixel_formats if pf is not None
        ]

        camera_caps = Gst.Caps.new_empty()

        for pixel_format in supported_pixel_formats:

            self.set_cam_node_val("PixelFormat", pixel_format.genicam, False)

            width_min, width_max = self.get_cam_node_range("Width")
            height_min, height_max = self.get_cam_node_range("Height")
            fr_min, fr_max = self.get_cam_node_range("AcquisitionFrameRate")

            camera_caps.append_structure(
                Gst.Structure(
                    pixel_format.cap_type,
                    format=pixel_format.gst,
                    width=Gst.IntRange(range(width_min, width_max)),
                    height=Gst.IntRange(range(height_min, height_max)),
                    framerate=Gst.FractionRange(
                        Gst.Fraction(*Gst.util_double_to_fraction(fr_min)),
                        Gst.Fraction(*Gst.util_double_to_fraction(fr_max)),
                    ),
                )
            )

        # Set the pixel format back to the starting format
        self.set_cam_node_val("PixelFormat", starting_pixel_format, False)

        return camera_caps

    # Camera helper function
    def start_streaming(self) -> bool:

        if not self.apply_caps_to_cam():
            return False

        try:
            self.image_acquirer.start_acquisition()
        except ValueError as ex:
            Gst.error(f"Error: {ex}")
            return False

        Gst.info("Acquisition Started")
        return True

    # GST function
    def do_set_caps(self, caps: Gst.Caps) -> bool:
        Gst.info("Setting caps")
        self.info.from_caps(caps)
        self.set_blocksize(self.info.size)
        return self.start_streaming()

    # GST function
    def do_get_caps(self, filter: Gst.Caps) -> Gst.Caps:
        Gst.info("Get Caps")
        caps = None

        if self.camera_caps is not None:
            caps = Gst.Caps.copy(self.camera_caps)
        else:
            caps = Gst.Caps.new_any()

        Gst.info(f"Avaliable caps: {caps.to_string()}")
        return caps

    # GST function
    def do_fixate(self, caps: Gst.Caps) -> Gst.Caps:
        Gst.info("Fixating caps")

        height = self.get_cam_node_val("Height")
        width = self.get_cam_node_val("Width")
        frame_rate = self.get_cam_node_val("AcquisitionFrameRate")

        structure = caps.get_structure(0).copy()
        structure.fixate_field_nearest_int("width", width)
        structure.fixate_field_nearest_int("height", height)
        structure.fixate_field_nearest_fraction("framerate", frame_rate, 1)

        new_caps = Gst.Caps.new_empty()
        new_caps.append_structure(structure)
        return new_caps.fixate()

    # GST function
    def do_start(self) -> bool:
        Gst.info("Starting")
        try:
            self.image_acquirer = ImageAcquirer()

            if not self.image_acquirer.init_device(
                device_serial=self.serial,
                device_index=(0 if self.serial is None else None),
            ):
                return False

            if not self.apply_properties_to_cam():
                return False

            self.camera_caps = self.get_camera_caps()

        except Exception as ex:
            Gst.error(f"Error: {ex}")
            return False
        return True

    # GST function
    def do_stop(self) -> bool:
        Gst.info("Stopping")
        try:
            del self.image_acquirer
        except Exception as ex:
            Gst.error(f"Error: {ex}")
        return True

    # GST function
    def do_get_times(self, buffer: Gst.Buffer) -> (int, int):
        end = 0
        start = 0
        if self.is_live:
            ts = buffer.pts
            if ts != Gst.CLOCK_TIME_NONE:
                duration = buffer.duration
                if duration != Gst.CLOCK_TIME_NONE:
                    end = ts + duration
                start = ts
        else:
            start = Gst.CLOCK_TIME_NONE
            end = Gst.CLOCK_TIME_NONE

        return start, end

    # GST function
    def do_gst_push_src_fill(self, buffer: Gst.Buffer) -> Gst.FlowReturn:
        try:
            image_buffer = gst_buffer_with_pad_to_ndarray(buffer, self.srcpad)
            image_buffer[:], image_timestamp_ns = self.image_acquirer.get_next_image(
                logger=Gst.warning
            )

            if self.timestamp_offset == 0:
                self.timestamp_offset = image_timestamp_ns
                self.previous_timestamp = image_timestamp_ns

            buffer.pts = image_timestamp_ns - self.timestamp_offset
            buffer.duration = image_timestamp_ns - self.previous_timestamp

            self.previous_timestamp = image_timestamp_ns

            Gst.log(
                f"Sending buffer of size: {image_buffer.shape} "
                f"timestamp offset: {buffer.pts // self.MILLISECONDS_PER_NANOSECOND}ms"
            )

        except Exception as ex:
            Gst.error(f"Error: {ex}")
            return Gst.FlowReturn.ERROR

        return Gst.FlowReturn.OK


# Register plugin to use it from command line
GObject.type_register(PySpinSrc)
__gstelementfactory__ = (PySpinSrc.GST_PLUGIN_NAME, Gst.Rank.NONE, PySpinSrc)
