import torch
import numpy as np
from torch.autograd import Variable
import torch.nn as nn
import torch.optim
import json
import torch.utils.data.sampler
import os
import glob
import random
import time
import torch.multiprocessing as mp
from tqdm import tqdm
import sys

import configs
import backbone
import data.feature_loader as feat_loader
from data.datamgr import SetDataManager
from methods.baselinetrain import BaselineTrain
from methods.baselinefinetune import BaselineFinetune
from methods.protonet import ProtoNet
from methods.matchingnet import MatchingNet
from methods.relationnet import RelationNet
from methods.maml import MAML
from methods.anil import ANIL
from methods.imaml_idcg import IMAML_IDCG
from methods.sharpmaml import SharpMAML

from io_utils import model_dict, parse_args, get_resume_file, get_best_file , get_assigned_file


def feature_evaluation(cl_data_file, model, n_way = 5, n_support = 5, n_query = 15, adaptation = False):
    class_list = cl_data_file.keys()
    select_class = random.choices(list(class_list),k=n_way)
    z_all  = []
    for cl in select_class:
        img_feat = cl_data_file[cl]
        perm_ids = np.random.permutation(len(img_feat)).tolist()
        z_all.append( [ np.squeeze( img_feat[perm_ids[i]]) for i in range(n_support+n_query) ] )     # stack each batch

    z_all = torch.from_numpy(np.array(z_all) )
   
    model.n_query = n_query
    if adaptation:
        scores  = model.set_forward_adaptation(z_all, is_feature = True)
    else:
        scores  = model.set_forward(z_all, is_feature = True)
    pred = scores.data.cpu().numpy().argmax(axis = 1)
    y = np.repeat(range( n_way ), n_query )
    acc = np.mean(pred == y)*100 
    return acc

if __name__ == '__main__':
    mp.set_start_method('spawn')
    result_dir = configs.ROOT_DIR + '/record' 
    if not os.path.exists(result_dir):
       os.makedirs(result_dir)

    

    params = parse_args('test')
    print(f'Applying StainNet stain normalisation......') if params.sn else print()

    acc_all = []
    iter_num = 600

    few_shot_params = dict(n_way = params.test_n_way , n_support = params.n_shot) 


    if params.method == 'baseline':
        model = BaselineFinetune( model_dict[params.model], **few_shot_params )
    elif params.method == 'baseline++':
        model = BaselineFinetune( model_dict[params.model], loss_type = 'dist', **few_shot_params )
    elif params.method == 'protonet':
        model = ProtoNet( model_dict[params.model], **few_shot_params )
    elif params.method == 'matchingnet':
        model = MatchingNet( model_dict[params.model], **few_shot_params )
    elif params.method in ['relationnet', 'relationnet_softmax']:
        if params.model == 'Conv4': 
            feature_model = backbone.Conv4NP
        elif params.model == 'Conv6': 
            feature_model = backbone.Conv6NP
        else:
            feature_model = lambda: model_dict[params.model]( flatten = False )
        loss_type = 'mse' if params.method == 'relationnet' else 'softmax'
        model           = RelationNet( feature_model, loss_type = loss_type , **few_shot_params )

    elif params.method in ['maml' , 'maml_approx', 'anil', 'imaml_idcg', 'sharpmaml']:

      backbone.ConvBlock.maml = True
      backbone.SimpleBlock.maml = True
      backbone.BottleneckBlock.maml = True
      backbone.ResNet.maml = True

      if params.method in ['maml', 'maml_approx']:
        model = MAML(  model_dict[params.model], approx = (params.method == 'maml_approx') , **few_shot_params )
     
      elif params.method == 'anil':
        model = ANIL(  model_dict[params.model], approx = False , **few_shot_params )

      elif params.method == 'imaml_idcg':
        assert params.model not in ['Conv4', 'Conv6','Conv4NP', 'Conv6NP', 'ResNet10'], 'imaml_idcg do not support non-ImageNet pretrained model'
        feature_backbone = lambda: model_dict[params.model]( flatten = True, method = params.method )
        model = IMAML_IDCG(  feature_backbone, approx = False , **few_shot_params )
        # model = IMAML_IDCG(  model_dict[params.model], approx = False , **few_shot_params )

      elif params.method == 'sharpmaml':
        model = SharpMAML(  model_dict[params.model], approx = False , **few_shot_params )


    else:
       raise ValueError('Unknown method')

    model = model.cuda()

    checkpoint_dir = '%s/checkpoints/%s/%s_%s' %(configs.save_dir, params.dataset, params.model, params.method)
    if params.train_aug:
        checkpoint_dir += f'_{params.train_aug}'
    if params.sn:
        checkpoint_dir += '_stainnet'

    if not params.method in ['baseline', 'baseline++'] :
        checkpoint_dir += '_%dway_%dshot' %( params.train_n_way, params.n_shot)


    if not params.method in ['baseline', 'baseline++'] : 
        if params.save_iter != -1:
            modelfile   = get_assigned_file(checkpoint_dir,params.save_iter)
        else:
            modelfile   = get_best_file(checkpoint_dir)
        if modelfile is not None:
            tmp = torch.load(modelfile)
            model.load_state_dict(tmp['state'])
            if hasattr(model, 'task_lr'):
                model.task_lr = tmp['task_lr']

    split = params.split
    if params.save_iter != -1:
        split_str = split + "_" +str(params.save_iter)
    else:
        split_str = split
    if params.method in ['maml', 'maml_approx', 'anil', 'imaml_idcg', 'sharpmaml']: #maml do not support testing with feature
        if 'Conv' in params.model:
            image_size = 84 
        elif 'EffNet' in params.model:
            image_size = 480 
        else:
            image_size = 224

     
        datamgr  = SetDataManager(image_size, n_eposide = iter_num, n_query = 15 , **few_shot_params)

        if params.dataset == 'cross_IDC_4x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_4x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['BCHI'] + split +'.json' 
        elif params.dataset == 'cross_IDC_10x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_10x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['BCHI'] + split +'.json'
        elif params.dataset == 'cross_IDC_20x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_20x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['BCHI'] + split +'.json'
        elif params.dataset == 'cross_IDC_40x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_40x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['BCHI'] + split +'.json'


        elif params.dataset == 'cross_IDC_4x_2':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_4x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_40x'] + split +'.json'
        elif params.dataset == 'cross_IDC_10x_2':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_10x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_40x'] + split +'.json'
        elif params.dataset == 'cross_IDC_20x_2':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_20x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_40x'] + split +'.json'
        elif params.dataset == 'cross_IDC_40x_2':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_40x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_40x'] + split +'.json'


        elif params.dataset == 'cross_IDC_4x_3':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_4x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_20x'] + split +'.json'
        elif params.dataset == 'cross_IDC_10x_3':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_10x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_20x'] + split +'.json'
        elif params.dataset == 'cross_IDC_20x_3':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_20x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_20x'] + split +'.json'
        elif params.dataset == 'cross_IDC_40x_3':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_40x'] + 'base.json' 
          else:
              loadfile  = configs.data_dir['PathoIDC_20x'] + split +'.json'


        elif params.dataset == 'long_tail_4x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_4x'] + 'base_long.json' 
          else:
              loadfile  = configs.data_dir['BreaKHis_4x'] + split + '_long.json'
        elif params.dataset == 'long_tail_10x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_10x'] + 'base_long.json' 
          else:
              loadfile  = configs.data_dir['BreaKHis_10x'] + split + '_long.json'
        elif params.dataset == 'long_tail_20x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_20x'] + 'base_long.json' 
          else:
              loadfile  = configs.data_dir['BreaKHis_20x'] + split + '_long.json'
        elif params.dataset == 'long_tail_40x':
          if split == 'base':
              loadfile = configs.data_dir['BreaKHis_40x'] + 'base_long.json' 
          else:
              loadfile  = configs.data_dir['BreaKHis_40x'] + split + '_long.json'

        else:    
           raise ValueError(f"Unsupported dataset: {params.dataset}")
           

        novel_loader     = datamgr.get_data_loader( loadfile, aug = 'none', sn = params.sn)

        if params.adaptation:
            model.task_update_num = 100 #We perform adaptation on MAML simply by updating more times.
        model.eval()
        acc_mean, acc_std = model.test_loop( novel_loader, return_std = True)

    else:
        novel_file = os.path.join( checkpoint_dir.replace("checkpoints","features"), split_str +".hdf5") #defaut split = novel, but you can also test base or val classes
        cl_data_file = feat_loader.init_loader(novel_file)

        for i in tqdm(range(iter_num)):     
            acc = feature_evaluation(cl_data_file, model, n_query = 15, adaptation = params.adaptation, **few_shot_params)
            acc_all.append(acc)

        acc_all  = np.asarray(acc_all)
        acc_mean = np.mean(acc_all)
        acc_std  = np.std(acc_all)
        print('%d Test Acc = %4.2f%% ± %4.2f%%' %(iter_num, acc_mean, 1.96* acc_std/np.sqrt(iter_num)))
        
    with open(os.path.join(result_dir, 'results.txt') , 'a') as f:
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime()) 
        aug_str = '-aug' if params.train_aug else ''
        aug_str += '-adapted' if params.adaptation else ''
        if params.method in ['baseline', 'baseline++'] :
            exp_setting = '%s-%s-%s-%s%s %sshot %sway_test' %(params.dataset, split_str, params.model, params.method, aug_str, params.n_shot, params.test_n_way )
        else:
            exp_setting = '%s-%s-%s-%s%s %sshot %sway_train %sway_test' %(params.dataset, split_str, params.model, params.method, aug_str , params.n_shot , params.train_n_way, params.test_n_way )
        acc_str = '%d Test Acc = %4.2f%% ± %4.2f%%' %(iter_num, acc_mean, 1.96* acc_std/np.sqrt(iter_num))
        f.write( 'Time: %s, Setting: %s, Acc: %s \n' %(timestamp,exp_setting,acc_str)  )
