#ref: https://github.com/ultralytics/yolov5/blob/master/detect.py

import argparse
import os
import platform
import sys
from pathlib import Path

import torch
import torch.nn as nn
import numpy as np
import cv2
import numpy as np
import rclpy
from rclpy.duration import Duration
from rclpy.node import Node
from rcl_interfaces.msg import ParameterDescriptor
from std_msgs.msg import String
from vision_msgs.msg import Detection2D
from vision_msgs.msg import Detection2DArray
from vision_msgs.msg import ObjectHypothesisWithPose
from pathlib import Path

FILE = Path(__file__).resolve()
ROOT = FILE.parents[0]

IMG_FORMATS = 'bmp', 'dng', 'jpeg', 'jpg', 'mpo', 'png', 'tif', 'tiff', 'webp', 'pfm'  # include image suffixes
VID_FORMATS = 'asf', 'avi', 'gif', 'm4v', 'mkv', 'mov', 'mp4', 'mpeg', 'mpg', 'ts', 'wmv'  # include video suffixes

def parse_opt():
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str, default=ROOT / 'yolov5s.pt', help='model path or triton URL')
    parser.add_argument('--source', type=str, default=ROOT / 'data/images', help='file/dir/URL/glob/screen/0(webcam)')
    parser.add_argument('--data', type=str, default=ROOT / 'data/coco128.yaml', help='(optional) dataset.yaml path')
    parser.add_argument('--imgsz', '--img', '--img-size', nargs='+', type=int, default=[640], help='inference size h,w')
    parser.add_argument('--conf-thres', type=float, default=0.25, help='confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='NMS IoU threshold')
    parser.add_argument('--max-det', type=int, default=1000, help='maximum detections per image')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img', action='store_true', help='show results')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--save-conf', action='store_true', help='save confidences in --save-txt labels')
    parser.add_argument('--save-crop', action='store_true', help='save cropped prediction boxes')
    parser.add_argument('--nosave', action='store_true', help='do not save images/videos')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --classes 0, or --classes 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--visualize', action='store_true', help='visualize features')
    parser.add_argument('--update', action='store_true', help='update all models')
    parser.add_argument('--project', default=ROOT / 'runs/detect', help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--line-thickness', default=3, type=int, help='bounding box thickness (pixels)')
    parser.add_argument('--hide-labels', default=False, action='store_true', help='hide labels')
    parser.add_argument('--hide-conf', default=False, action='store_true', help='hide confidences')
    parser.add_argument('--half', action='store_true', help='use FP16 half-precision inference')
    parser.add_argument('--dnn', action='store_true', help='use OpenCV DNN for ONNX inference')
    parser.add_argument('--vid-stride', type=int, default=1, help='video frame-rate stride')
    opt = parser.parse_args()
    opt.imgsz *= 2 if len(opt.imgsz) == 1 else 1  # expand
    #print_args(vars(opt))
    return opt


def smart_inference_mode(torch_1_9=True):
    # Applies torch.inference_mode() decorator if torch>=1.9.0 else torch.no_grad() decorator
    def decorate(fn):
        return (torch.inference_mode if torch_1_9 else torch.no_grad)()(fn)

    return decorate





class TRTBackend(nn.Module):
    def __init__(self, weights='yolov5s.pt', device=torch.device('cpu'), dnn=False, data=None, fp16=False, fuse=True):
        super().__init__()
        w = str(weights[0] if isinstance(weights, list) else weights)
        fp16 = True
        stride = 32  # default stride
        cuda = torch.cuda.is_available() and device.type != 'cpu'  # use CUDA

        import tensorrt as trt  # https://developer.nvidia.com/nvidia-tensorrt-download
        from collections import OrderedDict, namedtuple
        Binding = namedtuple('Binding', ('name', 'dtype', 'shape', 'data', 'ptr'))
        logger = trt.Logger(trt.Logger.INFO)
        with open(w, 'rb') as f, trt.Runtime(logger) as runtime:
            model = runtime.deserialize_cuda_engine(f.read())
        context = model.create_execution_context()
        bindings = OrderedDict()
        output_names = []
        fp16 = False  # default updated below
        dynamic = False
        for i in range(model.num_bindings):
            name = model.get_binding_name(i)
            dtype = trt.nptype(model.get_binding_dtype(i))
            if model.binding_is_input(i):
                if -1 in tuple(model.get_binding_shape(i)):  # dynamic
                    dynamic = True
                    context.set_binding_shape(i, tuple(model.get_profile_shape(0, i)[2]))
                if dtype == np.float16:
                    fp16 = True
            else:  # output
                output_names.append(name)
            shape = tuple(context.get_binding_shape(i))
            im = torch.from_numpy(np.empty(shape, dtype=dtype)).to(device)
            bindings[name] = Binding(name, dtype, shape, im, int(im.data_ptr()))
        binding_addrs = OrderedDict((n, d.ptr) for n, d in bindings.items())
        batch_size = bindings['images'].shape[0]  # if dynamic, this is instead max batch size
        #return dynamic, bindings

    def forward(self, im, augment=False, visualize=False):
        b, ch, h, w = im.shape  # batch, channel, height, width
        if self.fp16 and im.dtype != torch.float16:
            im = im.half()  # to FP16
        
        if self.dynamic and im.shape != self.bindings['images'].shape:
            i = self.model.get_binding_index('images')
            self.context.set_binding_shape(i, im.shape)  # reshape if dynamic
            self.bindings['images'] = self.bindings['images']._replace(shape=im.shape)
            for name in self.output_names:
                i = self.model.get_binding_index(name)
                self.bindings[name].data.resize_(tuple(self.context.get_binding_shape(i)))
        s = self.bindings['images'].shape
        assert im.shape == s, f"input size {im.shape} {'>' if self.dynamic else 'not equal to'} max model size {s}"
        self.binding_addrs['images'] = int(im.data_ptr())
        self.context.execute_v2(list(self.binding_addrs.values()))
        y = [self.bindings[x].data for x in sorted(self.output_names)]

        if isinstance(y, (list, tuple)):
            return self.from_numpy(y[0]) if len(y) == 1 else [self.from_numpy(x) for x in y]
        else:
            return self.from_numpy(y)

    def from_numpy(self, x):
        return torch.from_numpy(x).to(self.device) if isinstance(x, np.ndarray) else x

    

# def main(opt):
#     #check_requirements(exclude=('tensorboard', 'thop'))
#     run(**vars(opt))

# if __name__ == "__main__":
#     opt = parse_opt()
#     main(opt)
# weights=ROOT / 'yolov5s.pt',  # model path or triton URL
#         source=ROOT / 'data/images',  # file/dir/URL/glob/screen/0(webcam)
#         data=ROOT / 'data/coco128.yaml',  # dataset.yaml path
#         imgsz=(640, 640),  # inference size (height, width)
#         conf_thres=0.25,  # confidence threshold
#         iou_thres=0.45,  # NMS IOU threshold
#         max_det=1000,  # maximum detections per image
#         device='',  # cuda device, i.e. 0 or 0,1,2,3 or cpu
#         view_img=False,  # show results
#         save_txt=False,  # save results to *.txt
#         # save_conf=False,  # save confidences in --save-txt labels
#         # save_crop=False,  # save cropped prediction boxes
#         nosave=False,  # do not save images/videos
#         classes=None,  # filter by class: --class 0, or --class 0 2 3
#         agnostic_nms=False,  # class-agnostic NMS
#         augment=False,  # augmented inference
#         visualize=False,  # visualize features
#         #update=False,  # update all models
#         project=ROOT / 'runs/detect',  # save results to project/name
#         name='exp',  # save results to project/name
#         # exist_ok=False,  # existing project/name ok, do not increment
#         # line_thickness=3,  # bounding box thickness (pixels)
#         # hide_labels=False,  # hide labels
#         # hide_conf=False,  # hide confidences
#         half=False,  # use FP16 half-precision inference
#         #dnn=False,  # use OpenCV DNN for ONNX inference
#         vid_stride=1,  # video frame-rate stride
@smart_inference_mode()
def trtdetectfun(paramdict):
    source = str(source)
    save_img = not nosave and not source.endswith('.txt')  # save inference images
    is_file = Path(source).suffix[1:] in (IMG_FORMATS + VID_FORMATS)
    is_url = source.lower().startswith(('rtsp://', 'rtmp://', 'http://', 'https://'))
    webcam = source.isnumeric() or source.endswith('.streams') or (is_url and not is_file)
    screenshot = source.lower().startswith('screen')
    # if is_url and is_file:
    #     source = check_file(source)  # download

    # Directories
    save_dir = Path(project) / name #increment_path(Path(project) / name, exist_ok=exist_ok)  # increment run
    (save_dir / 'labels' if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir

    if torch.cuda.is_available():
        print(torch.cuda.device_count())
        device=torch.device('cuda:0')
    else:
        device=torch.device('cpu')
    
    # model = TRTBackend(weights, device=device, dnn=dnn, data=data, fp16=half)
    # stride, names, pt = model.stride, model.names, model.pt

    # # Dataloader
    # bs = 1  # batch_size
    # im0 = cv2.imread(path)  # BGR
    # img_size=640
    # stride=32
    # auto=True
    # im = letterbox(im0, img_size, stride=stride, auto=auto)[0]  # padded resize
    # im = im.transpose((2, 0, 1))[::-1]  # HWC to CHW, BGR to RGB
    # im = np.ascontiguousarray(im)  # contiguous

    # im = torch.from_numpy(im).to(model.device)
    # im = im.half() if model.fp16 else im.float()  # uint8 to fp16/32
    # im /= 255  # 0 - 255 to 0.0 - 1.0
    # if len(im.shape) == 3:
    #     im = im[None]  # expand for batch dim

    # # Run inference
    # model.warmup(imgsz=(1 if pt or model.triton else bs, 3, *imgsz))  # warmup
    # pred = model(im, augment=augment, visualize=visualize)
    #pred = non_max_suppression(pred, conf_thres, iou_thres, classes, agnostic_nms, max_det=max_det)



class TRTDetectNode(Node):
    def __init__(self, name='trtdetect_node'):
        super().__init__(name)
        param_names = ['weights', 'source', 'imgsz', 'conf_thres', 'iou_thres', 'max_det', \
            'device', 'view_img', 'save_txt', 'nosave', 'classes', 'agnostic_nms', 'augment', \
                'visualize', 'project', 'name', 'half', 'vid_stride']
        self.params_config = {}
        my_parameter_descriptor = ParameterDescriptor(dynamic_typing=True)#(description='This parameter is mine!')
        for param_name in param_names:
            self.declare_parameter(param_name, None, my_parameter_descriptor)
            try:
                self.params_config[param_name] = self.get_parameter(
                    param_name).value
            except rclpy.exceptions.ParameterUninitializedException:
                self.params_config[param_name] = None
        imgsz=self.params_config['imgsz']
        newimgsz=(imgsz[0], imgsz[1])
        self.params_config['imgsz']=newimgsz
        # self.declare_parameters(
        #     namespace='',
        #     parameters=[
        #         ('conf_thres', rclpy.Parameter.Type.DOUBLE),
        #         ('iou_thres', rclpy.Parameter.Type.DOUBLE),
        #         ('max_det', rclpy.Parameter.Type.INTEGER)
        #     ])
    
        
        # Create the publisher. This publisher will publish a Detection2DArray message
        # to topic object_detections. The queue size is 10 messages.
        self.publisher_ = self.create_publisher(
            Detection2DArray, 'object_detections', 10)
        
        #Detection function
        print(self.params_config)
        detections = trtdetectfun(self.params_config)

        # Publish the message to the topic
        self.publisher_.publish(detections)
        

def main(args=None):
    try:
        rclpy.init(args=args)
        node = TRTDetectNode('trtdetect_node')
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()