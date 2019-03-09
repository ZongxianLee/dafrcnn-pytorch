# --------------------------------------------------------
# Pytorch multi-GPU Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
# Domain Adaptive Faster R-CNN
# Written by Divyam Anshumaan
# Refined by Zongxian_lee
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim

import torchvision.transforms as transforms
from torch.utils.data.sampler import Sampler

from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.utils.net_utils import weights_normal_init, save_net, load_net, \
      adjust_learning_rate, save_checkpoint, clip_gradient

from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet

# domain adaptation import
from model.faster_rcnn.domain_adapt import *
from scipy.misc import imread
import cv2
from model.utils.blob import im_list_to_blob

os.environ["CUDA_VISIBLE_DEVICES"] = "0"
pwd = os.getcwd()


def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
    parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='pascal_voc', type=str)
####################################################################################################
# Domain Adaptation
    parser.add_argument('--da', dest='da',
                      default=True, type=bool,
                      help='Include domain adaptation')

    parser.add_argument('--adaption_lr', dest='adaption_lr',
                      default=True, type=bool,
                      help='modify gradient reverse')

    parser.add_argument('--src', dest='src_dataset',
                      default='city', type=str,
                      help='source dataset')
    parser.add_argument('--tar', dest='tar_dataset',
                      default='fcity', type=str,
                      help='target dataset')

    parser.add_argument('--ignore_filter', dest='ignore_filter',
                      default= False, type=bool,
                      help='ignore filter warning')
####################################################################################################

    parser.add_argument('--net', dest='net',
                    help='vgg16, res101',
                    default='res101', type=str)
    parser.add_argument('--start_epoch', dest='start_epoch',
                      help='starting epoch',
                      default=1, type=int)
    parser.add_argument('--epochs', dest='max_epochs',
                      help='number of epochs to train',
                      default=20, type=int)
    parser.add_argument('--disp_interval', dest='disp_interval',
                      help='number of iterations to display',
                      default=100, type=int)
    parser.add_argument('--checkpoint_interval', dest='checkpoint_interval',
                      help='number of iterations to display',
                      default=10000, type=int)

    parser.add_argument('--save_dir', dest='save_dir',
                      help='directory to save models', default= pwd + "/models",
                      type=str)
    parser.add_argument('--nw', dest='num_workers',
                      help='number of worker to load data',
                      default=0, type=int)
    parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      default=True, action='store_true')
    parser.add_argument('--ls', dest='large_scale',
                      help='whether use large imag scale',
                      action='store_true')
    parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
    parser.add_argument('--bs', dest='batch_size',
                      help='batch_size',
                      default=2, type=int)
    parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')

# config optimization
    parser.add_argument('--o', dest='optimizer',
                      help='training optimizer',
                      default="sgd", type=str)
    parser.add_argument('--lr', dest='lr',
                      help='starting learning rate',
                      default=0.001, type=float)

    parser.add_argument('--lr_decay_step', dest='lr_decay_step',
                      help='step to do learning rate decay, unit is epoch',
                      default=5, type=int)
    parser.add_argument('--lr_decay_gamma', dest='lr_decay_gamma',
                      help='learning rate decay ratio',
                      default=0.1, type=float)

# set training session
    parser.add_argument('--s', dest='session',
                      help='training session',
                      default=1, type=int)

# resume trained model
    parser.add_argument('--r', dest='resume',
                      help='resume checkpoint or not',
                      default=False, type=bool)
    parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
    parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load model',
                      default=1, type=int)
    parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load model',
                      default=0, type=int)
# log and diaplay
    parser.add_argument('--use_tfboard', dest='use_tfboard',
                      help='whether use tensorflow tensorboard',
                      default=False, type=bool)

    args = parser.parse_args()
    return args
####################################################################################################
def _get_image_blob(im):

    """Converts an image into a network input.
    Arguments:
    im (ndarray): a color image in BGR order
    Returns:
    blob (ndarray): a data blob holding an image pyramid
    im_scale_factors (list): list of image scales (relative to im) used
      in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []

    for target_size in cfg.TEST.SCALES:
        im_scale = float(target_size) / float(im_size_min)
# Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
            im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
        im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)
        im_scale_factors.append(im_scale)
        processed_ims.append(im)

# Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)
####################################################################################################
class sampler(Sampler):
    def __init__(self, train_size, batch_size):
        self.num_data = train_size
        self.num_per_batch = int(train_size / batch_size)
        self.batch_size = batch_size
        self.range = torch.arange(0,batch_size).view(1, batch_size).long()
        self.leftover_flag = False
        if train_size % batch_size:
            self.leftover = torch.arange(self.num_per_batch*batch_size, train_size).long()
            self.leftover_flag = True

    def __iter__(self):
        rand_num = torch.randperm(self.num_per_batch).view(-1,1) * self.batch_size
        self.rand_num = rand_num.expand(self.num_per_batch, self.batch_size) + self.range

        self.rand_num_view = self.rand_num.view(-1)

        if self.leftover_flag:
            self.rand_num_view = torch.cat((self.rand_num_view, self.leftover),0)

        return iter(self.rand_num_view)

    def __len__(self):
        return self.num_data

####################################################################################################

if __name__ == '__main__':

    args = parse_args()

    print('Called with args:')
    print(args)

    if args.use_tfboard:
        from model.utils.logger import Logger
# Set the logger
        logger = Logger('./logs')
# Add custom dataset add cfgs from da-faster-rcnn
# Make sure you change the imdb_name in factory.py
    """
    Dummy format:

    args.src_dataset == '$YOUR_DATASET_NAME'
    args.src_imdb_name = '$YOUR_DATASET_NAME_2007_trainval'
    args.src_imdbval_name = '$YOUR_DATASET_NAME_2007_test'
    args.set_cfgs = [...]
    """
    if args.src_dataset == "city":
        args.src_imdb_name = "city_2007_trainval"
        args.src_imdbval_name = "city_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]', 'MAX_NUM_GT_BOXES', '20']

    elif args.src_dataset == "fcity":
        args.src_imdb_name = "fcity_2007_trainval"
        args.src_imdbval_name = "city_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]', 'MAX_NUM_GT_BOXES', '20']

    if args.tar_dataset == "city":
        args.tar_imdb_name = "city_2007_trainval"
        args.tar_imdbval_name = "fcity_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]', 'MAX_NUM_GT_BOXES', '20']

    elif args.tar_dataset == "fcity":
        args.tar_imdb_name = "fcity_2007_trainval"
        args.tar_imdbval_name = "fcity_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]', 'MAX_NUM_GT_BOXES', '20']

    args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net)

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    print('Using config:')
    pprint.pprint(cfg)
    np.random.seed(cfg.RNG_SEED)

####################################################################################################
    if torch.cuda.is_available() and not args.cuda:
        print('WARNING: You have a cuda device, so you should probably run with --cuda')

    cfg.TRAIN.USE_FLIPPED = True
    cfg.USE_GPU_NMS = args.cuda
    # src, tar dataloaders

    src_imdb, src_roidb, src_ratio_list, src_ratio_index = combined_roidb(args.src_imdb_name, domain='src')
    train_size_src = len(src_roidb)

    # Modify train size. Make sure both are of same size.
    # Modify training loop to continue giving src loss after tar is done.
    train_size = train_size_src
    src_dataset = roibatchLoader(src_roidb, src_ratio_list, src_ratio_index, 1, src_imdb.num_classes, training=True)

    sampler_batch_src = sampler(train_size, args.batch_size)
    sampler_batch_tar = sampler(train_size, args.batch_size)

    src_dataloader = torch.utils.data.DataLoader(src_dataset, sampler=sampler_batch_src, batch_size=1)

    print('{:d} source roidb entries'.format(len(src_roidb)))

    # Update output_dir for testing!

    output_dir = args.save_dir + "/" + args.net + "/" + args.src_dataset
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # initilize the tensor holder here.
    # initialize src, tar tensors

    src_img_data = torch.FloatTensor(1)
    src_img_info = torch.FloatTensor(1)
    src_num_boxes = torch.LongTensor(1)
    src_gt_boxes = torch.FloatTensor(1)

    tar_img_data = torch.FloatTensor(1)
    tar_img_info = torch.FloatTensor(1)
    tar_num_boxes = torch.LongTensor(1)
    tar_gt_boxes = torch.FloatTensor(1)

  ##################################################################################################

    # ship to cuda
    if args.cuda:
        # domain
        src_img_data = src_img_data.cuda()
        src_img_info = src_img_info.cuda()
        src_num_boxes = src_num_boxes.cuda()
        src_gt_boxes = src_gt_boxes.cuda()

        tar_img_data = tar_img_data.cuda()
        tar_img_info = tar_img_info.cuda()
        tar_num_boxes = tar_num_boxes.cuda()
        tar_gt_boxes = tar_gt_boxes.cuda()

    # UPDATED: same as declaring tensor object. We are using v0.2.0_3!
    # make variable
    src_img_data = Variable(src_img_data)
    src_img_info = Variable(src_img_info)
    src_num_boxes = Variable(src_num_boxes)
    src_gt_boxes = Variable(src_gt_boxes)

    tar_im_data = Variable(tar_img_data)
    tar_im_info = Variable(tar_img_info)
    tar_num_boxes = Variable(tar_num_boxes)
    tar_gt_boxes = Variable(tar_gt_boxes)

    if args.cuda:
        cfg.CUDA = True

    # Initialize domain classifiers here.
    if args.da == True:
        d_cls_image = d_cls_image()
        d_cls_inst = d_cls_inst()

####################################################################################################

    if args.cuda:
        cfg.CUDA = True

# initilize the network here.
    if args.net == 'vgg16':
        fasterRCNN = vgg16(src_imdb.classes, pretrained=True, class_agnostic=args.class_agnostic)
    elif args.net == 'res101':
        fasterRCNN = resnet(src_imdb.classes, 101, pretrained=True, class_agnostic=args.class_agnostic)
    elif args.net == 'res50':
        fasterRCNN = resnet(src_imdb.classes, 50, pretrained=True, class_agnostic=args.class_agnostic)
    elif args.net == 'res152':
        fasterRCNN = resnet(src_imdb.classes, 152, pretrained=True, class_agnostic=args.class_agnostic)
    else:
      print("network is not defined")
      pdb.set_trace()

####################################################################################################

    fasterRCNN.create_architecture()
    lr = cfg.TRAIN.LEARNING_RATE
    lr = args.lr

#tr_momentum = cfg.TRAIN.MOMENTUM
#tr_momentum = args.momentum

    params = []
    for key, value in dict(fasterRCNN.named_parameters()).items():
        if value.requires_grad:
            if 'bias' in key:
                params += [{'params':[value],'lr':lr*(cfg.TRAIN.DOUBLE_BIAS + 1), \
                    'weight_decay': cfg.TRAIN.BIAS_DECAY and cfg.TRAIN.WEIGHT_DECAY or 0}]
            else:
                params += [{'params':[value],'lr':lr, 'weight_decay': cfg.TRAIN.WEIGHT_DECAY}]

    if args.optimizer == "adam":
        lr = lr * 0.1
        optimizer = torch.optim.Adam(params)
# domain optimizers
        d_image_opt = torch.optim.Adam(d_cls_image.parameters(), params['lr'], params['weight_decay'])
        d_inst_opt = torch.optim.Adam(d_cls_inst.parameters(), params['lr'], params['weight_decay'])

    elif args.optimizer == "sgd":
        optimizer = torch.optim.SGD(params, momentum=cfg.TRAIN.MOMENTUM)
# domain optimizers
        d_image_opt = torch.optim.SGD(d_cls_image.parameters(), lr=lr*(cfg.TRAIN.DOUBLE_BIAS + 1), weight_decay=cfg.TRAIN.WEIGHT_DECAY, momentum=cfg.TRAIN.MOMENTUM)
        d_inst_opt = torch.optim.SGD(d_cls_inst.parameters(), lr=lr*(cfg.TRAIN.DOUBLE_BIAS + 1), weight_decay=cfg.TRAIN.WEIGHT_DECAY, momentum=cfg.TRAIN.MOMENTUM)

        #d_image_opt = torch.optim.SGD(d_cls_image.parameters(), lr=args.lr, weight_decay=cfg.TRAIN.WEIGHT_DECAY, momentum=cfg.TRAIN.MOMENTUM)
        #d_inst_opt = torch.optim.SGD(d_cls_inst.parameters(), lr=args.lr, weight_decay=cfg.TRAIN.WEIGHT_DECAY, momentum=cfg.TRAIN.MOMENTUM)

# Set domain image level loss criteria
    def d_criteria(d_x, label):
        policy = torch.nn.CrossEntropyLoss()
        loss = policy(d_x, label)
        return loss

# Initialize consistency regularization
    d_cst_src_loss = 0
    d_cst_tar_loss = 0
####################################################################################################

    if args.resume:
        load_name = os.path.join(output_dir,
          'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))
        print("loading checkpoint %s" % (load_name))
        checkpoint = torch.load(load_name)
        args.session = checkpoint['session']
        args.start_epoch = checkpoint['epoch']
        fasterRCNN.load_state_dict(checkpoint['model'])
        d_cls_image.load_state_dict(checkpoint['d_cls_image'])
        d_cls_inst.load_state_dict(checkpoint['d_inst_img'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        d_image_opt.load_state_dict(checkpoint['d_image_opt'])
        d_inst_opt.load_state_dict(checkpoint['d_inst_opt'])
        lr = optimizer.param_groups[0]['lr']
        if 'pooling_mode' in checkpoint.keys():
            cfg.POOLING_MODE = checkpoint['pooling_mode']
        print("loaded checkpoint %s" % (load_name))

    if args.cuda:
      fasterRCNN.cuda()
      d_cls_image.cuda()
      d_cls_inst.cuda()


    if args.mGPUs:
        fasterRCNN = nn.DataParallel(fasterRCNN)
        #d_cls_image = nn.DataParallel(d_cls_image)
        #d_cls_inst = nn.DataParallel(d_cls_inst)



    iters_per_epoch = int(train_size / args.batch_size)

####################################################################################################
# Domain Adaptive version
####################################################################################################
# Modify epoch cycle
    for epoch in range(args.start_epoch, args.max_epochs + 1):
        start_steps = epoch * len(src_dataloader)
        total_steps = args.max_epochs * len(src_dataloader)
        frcnn_loss_tmp = 0
        adaption_loss_tmp = 0
        is_flipped = 0

# setting to train mode
        fasterRCNN.train()
# set domain classifiers to train mode
        d_cls_image.train()
        d_cls_inst.train()
###################################################################################################

        tar_name_dir = pwd + '/data/DA_Detection_SampleData/Target/VOCdevkit/VOC2007/ImageSets/Main/test.txt'# Insert target testset path here
        tar_img_dir = pwd + '/data/DA_Detection_SampleData/Target/VOCdevkit/VOC2007/JPEGImages'# Insert target image directory here

        with open(tar_name_dir) as f:
            namelist = f.read().splitlines()
        imglist = os.listdir(tar_img_dir)
        #num_names = len(namelist)

        #for i in range(num_names):
        #    a = namelist[i]+'.jpg'
        #    imglist.remove(a)
        #    print('removed', a)
        num_images = len(imglist)
        #print(num_images)
###################################################################################################
        loss_temp = 0
        start = time.time()

        if epoch % (args.lr_decay_step + 1) == 0:
            adjust_learning_rate(optimizer, args.lr_decay_gamma)
            adjust_learning_rate(d_image_opt)
            adjust_learning_rate(d_inst_opt)
            lr *= args.lr_decay_gamma
            print('learning rate decayed to', lr)

        src_data_iter = iter(src_dataloader)

        for step in range(iters_per_epoch):
# add scoring for normal faster rcnn

            src_data = next(src_data_iter)
            src_img_data.data.resize_(src_data[0].size()).copy_(src_data[0])
            src_img_info.data.resize_(src_data[1].size()).copy_(src_data[1])
            src_gt_boxes.data.resize_(src_data[2].size()).copy_(src_data[2])
            src_num_boxes.data.resize_(src_data[3].size()).copy_(src_data[3])

# EXTRACT FEATURE MAP AND ROI MAP HERE
# Set gradient to zero...
            fasterRCNN.zero_grad()
# Domain classifiers
            d_cls_image.zero_grad()
            d_cls_inst.zero_grad()

            src_rois, \
            src_cls_prob, src_bbox_pred, \
            src_rpn_loss_cls, src_rpn_loss_box, \
            src_RCNN_loss_cls, src_RCNN_loss_bbox,\
            src_rois_label, src_feat_map, \
            src_roi_pool = fasterRCNN(src_img_data, src_img_info, src_gt_boxes, src_num_boxes, is_target=False)

            #faster_rcnn_loss = src_rpn_loss_cls.mean() + src_rpn_loss_box.mean() + src_RCNN_loss_cls.mean() + src_RCNN_loss_bbox.mean()
            #frcnn_loss_tmp += faster_rcnn_loss.item()
            #if step % args.disp_interval == 0:
            #  if step > 0:
            #    frcnn_loss_tmp /= (args.disp_interval + 1)
            #  print("SRC_FasterRCNN_Loss: %.4f" % frcnn_loss_tmp)

####################################################################################################
            tar_im_counter = step%num_images
            tar_im_file = os.path.join(tar_img_dir, imglist[tar_im_counter])
            tar_im_in = np.array(imread(tar_im_file))
# Use flipped images...
            if tar_im_counter==0:
                is_flipped = (is_flipped+1)%2
                print('flipped targets...', is_flipped)
                if is_flipped==0:
                    tar_im_in = np.fliplr(tar_im_in)

            if len(tar_im_in.shape) == 2:
                tar_im_in = tar_im_in[:,:,np.newaxis]
                tar_im_in = np.concatenate((tar_im_in,tar_im_in,tar_im_in), axis=2)
# rgb -> bgr
            tar_im = tar_im_in[:,:,::-1]

            blobs, tar_im_scales = _get_image_blob(tar_im)
            assert len(tar_im_scales) == 1, " only single image batch implemented"
            tar_im_blob = blobs
            tar_im_info_np = np.array([[tar_im_blob.shape[1], tar_im_blob.shape[2], tar_im_scales[0]]], dtype=np.float32)

            tar_im_data_pt = torch.from_numpy(tar_im_blob)
            tar_im_data_pt = tar_im_data_pt.permute(0, 3, 1, 2)
            tar_im_info_pt = torch.from_numpy(tar_im_info_np)

            tar_im_data.data.resize_(tar_im_data_pt.size()).copy_(tar_im_data_pt)
            tar_im_info.data.resize_(tar_im_info_pt.size()).copy_(tar_im_info_pt)
            tar_gt_boxes.data.resize_(1, 1, 5).zero_()
            tar_num_boxes.data.resize_(1).zero_()

            tar_feat_map, tar_roi_pool = fasterRCNN(tar_im_data, tar_im_info, tar_gt_boxes, tar_num_boxes, is_target=True)
###################################################################################################
# Add domain adaption elements
# setup hyperparameter
            p = float(step + start_steps) / total_steps
            constant = 2. / (1. + np.exp(-10 * p)) - 1
###################################################################################################
            d_cls_image.set_beta(constant)
            d_cls_inst.set_beta(constant)


            src_d_img_score = d_cls_image(src_feat_map)
            src_d_inst_score = d_cls_inst(src_roi_pool)
            tar_d_img_score = d_cls_image(tar_feat_map)
            tar_d_inst_score = d_cls_inst(tar_roi_pool)

            s1 = list(src_d_img_score.size())[0]
            s2 = list(tar_d_img_score.size())[0]
            s3 = list(src_d_inst_score.size())[0]
            s4 = list(tar_d_inst_score.size())[0]

            src_img_label = Variable(torch.zeros(s1).long()).cuda()
            src_inst_label = Variable(torch.zeros(s3).long()).cuda()
            tar_img_label = Variable(torch.ones(s2).long()).cuda()
            tar_inst_label = Variable(torch.ones(s4).long()).cuda()

            src_d_img_loss = d_criteria(src_d_img_score,  src_img_label)
            src_d_inst_loss = d_criteria(src_d_inst_score, src_inst_label)
            tar_d_img_loss = d_criteria(tar_d_img_score, tar_img_label)
            tar_d_inst_loss = d_criteria(tar_d_inst_score, tar_inst_label)

            d_img_loss = src_d_img_loss + tar_d_img_loss
            d_inst_loss = src_d_inst_loss + tar_d_inst_loss

# Feature map representation: 1 x 1024 x H x W
            src_feat_map_dim = list(src_feat_map.size())[1]*list(src_feat_map.size())[2]*list(src_feat_map.size())[3]
            tar_feat_map_dim = list(tar_feat_map.size())[1]*list(tar_feat_map.size())[2]*list(tar_feat_map.size())[3]

            src_d_cst_loss = consistency_reg(src_feat_map_dim, src_d_img_score, src_d_inst_score, domain='src')
            tar_d_cst_loss = consistency_reg(tar_feat_map_dim, tar_d_img_score, tar_d_inst_score, domain='tar')

            d_cst_loss = src_d_cst_loss + tar_d_cst_loss

# Add domain loss
            faster_rcnn_loss = src_rpn_loss_cls.mean() + src_rpn_loss_box.mean() + src_RCNN_loss_cls.mean() + src_RCNN_loss_bbox.mean() 
            frcnn_loss_tmp += faster_rcnn_loss.item()
            
            adaption_loss = d_img_loss.mean() + d_inst_loss.mean() + d_cst_loss.mean()
            adaption_loss_tmp += adaption_loss.item()

            loss = faster_rcnn_loss + (0.1)*(adaption_loss)
            loss_temp += loss.item()

# frcnn backward
            optimizer.zero_grad()
# domain backward
            d_inst_opt.zero_grad()
            d_image_opt.zero_grad()

            loss.backward()

            if args.net == "vgg16":
                clip_gradient(fasterRCNN, 10.)

# frcnn optimizer update
            optimizer.step()
# domain optimizer update
            d_inst_opt.step()
            d_image_opt.step()

###################################################################################################

            if step % args.disp_interval == 0:
                end = time.time()
                if step > 0:
                    loss_temp /= (args.disp_interval + 1)
                    adaption_loss_tmp /= (args.disp_interval + 1)
                    frcnn_loss_tmp /= (args.disp_interval +1)
                if args.mGPUs:
                    src_loss_rpn_cls = src_rpn_loss_cls.mean().item()
                    src_loss_rpn_box = src_rpn_loss_box.mean().item()
                    src_loss_rcnn_cls = src_RCNN_loss_cls.mean().item()
                    src_loss_rcnn_box = src_RCNN_loss_bbox.mean().item()
                    src_fg_cnt = torch.sum(src_rois_label.data.ne(0))
                    src_bg_cnt = src_rois_label.data.numel() - src_fg_cnt

                    src_loss_d_img = src_d_img_loss.mean().item()
                    src_loss_d_inst = src_d_inst_loss.mean().item()
                    src_loss_d_cst = src_d_cst_loss.mean().item()
                    tar_loss_d_img = tar_d_img_loss.mean().item()
                    tar_loss_d_inst = tar_d_inst_loss.mean().item()
                    tar_loss_d_cst = tar_d_cst_loss.mean().item()

                else:
                    src_loss_rpn_cls = src_rpn_loss_cls.item()
                    src_loss_rpn_box = src_rpn_loss_box.item()
                    src_loss_rcnn_cls = src_RCNN_loss_cls.item()
                    src_loss_rcnn_box = src_RCNN_loss_bbox.item()
                    src_fg_cnt = torch.sum(src_rois_label.data.ne(0))
                    src_bg_cnt = src_rois_label.data.numel() - src_fg_cnt

                    src_loss_d_img = src_d_img_loss.item()
                    src_loss_d_inst = src_d_inst_loss.item()
                    src_loss_d_cst = src_d_cst_loss.item()
                    tar_loss_d_img = tar_d_img_loss.item()
                    tar_loss_d_inst = tar_d_inst_loss.item()
                    tar_loss_d_cst = tar_d_cst_loss.item()

###################################################################################################
                print("src_d_cst_loss: %.4f"  % src_loss_d_cst)
                print("tar_d_cst_loss: %.4f"  % tar_loss_d_cst)
                print("SRC_FasterRCNN_Loss: %.4f" % frcnn_loss_tmp)
                print("adaption_loss: %.4f" % adaption_loss_tmp)

                print("[session %d][epoch %2d][iter %4d/%4d]\nloss: %.4f, lr: %.2e" \
                                      % (args.session, epoch, step, iters_per_epoch, loss_temp, lr))
                print("\t\t\tsrc_fg/src_bg=(%d/%d), time cost: %f" % (src_fg_cnt, src_bg_cnt, end-start))
                print("\t\t\tsrc_rpn_cls: %.4f,\nsrc_rpn_box: %.4f,\nsrc_rcnn_cls: %.4f,\nsrc_rcnn_box %.4f,\nsrc_d_img_loss: %.4f,\nsrc_d_inst_loss: %.4f,\nsrc_d_cst_loss: %.4f,\ntar_d_img_loss: %.4f,\ntar_d_inst_loss: %.4f,\ntar_d_cst_loss: %.4f\n" % (src_loss_rpn_cls, src_loss_rpn_box, src_loss_rcnn_cls, src_loss_rcnn_box, src_loss_d_img, src_loss_d_inst, src_loss_d_cst, tar_loss_d_img, tar_loss_d_inst, tar_loss_d_cst))
                print('\n')
                print('\n')
                loss_temp = 0
                frcnn_loss_tmp = 0
                adaption_loss_tmp = 0
                start = time.time()

####################################################################################################

        if args.mGPUs:
            save_name = os.path.join(output_dir, 'faster_rcnn_{}_{}_{}.pth'.format(args.session, epoch, step))
            save_checkpoint({
                'session': args.session,
                'epoch': epoch + 1,
                'model': fasterRCNN.module.state_dict(),
                'd_cls_image': d_cls_image.module.state_dict(),
                'd_cls_inst': d_cls_inst.module.state_dict(),
                'optimizer': optimizer.state_dict(),
                'd_image_opt': d_image_opt.state_dict(),
		            'd_inst_opt': d_inst_opt.state_dict(),
                'pooling_mode': cfg.POOLING_MODE,
                'class_agnostic': args.class_agnostic,
            }, save_name)
        else:
            save_name = os.path.join(output_dir, 'faster_rcnn_{}_{}_{}.pth'.format(args.session, epoch, step))
            save_checkpoint({
                'session': args.session,
                'epoch': epoch + 1,
                'model': fasterRCNN.state_dict(),
		            'd_cls_image': d_cls_image.state_dict(),
                'd_cls_inst': d_cls_inst.state_dict(),
                'optimizer': optimizer.state_dict(),
                'd_image_opt': d_image_opt.state_dict(),
                'd_inst_opt': d_inst_opt.state_dict(),
                'optimizer': optimizer.state_dict(),
                'pooling_mode': cfg.POOLING_MODE,
                'class_agnostic': args.class_agnostic,
            }, save_name)
        print('save model: {}'.format(save_name))

        end = time.time()
        print(end - start)
