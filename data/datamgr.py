# This code is modified from https://github.com/facebookresearch/low-shot-shrink-hallucinate

import torch
from PIL import Image
import numpy as np
import torchvision.transforms as transforms
from torchvision.transforms import AutoAugment, AutoAugmentPolicy, InterpolationMode, RandAugment, AugMix
from torchvision.transforms import v2
from torch.utils.data import default_collate
import data.additional_transforms as add_transforms
from data.stainnet_transform import StainNetTransform
from data.dataset import SimpleDataset, SetDataset, EpisodicBatchSampler
from abc import abstractmethod
import os
        


class TransformLoader:
    def __init__(self, image_size, normalize_param=None, jitter_param=None):
        self.image_size = image_size
        self.normalize_param = normalize_param or dict(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        self.jitter_param = jitter_param or dict(Brightness=0.4, Contrast=0.4, Color=0.4)

    
    def parse_transform(self, transform_type):
        if transform_type=='ImageJitter':
            method = add_transforms.ImageJitter( self.jitter_param )
            return method

        elif transform_type=='StainNetTransform':
            method = StainNetTransform()
            return method

        elif transform_type == 'AutoAugment':
            policy = AutoAugmentPolicy.IMAGENET  # You can change this to another policy if needed
            interpolation = InterpolationMode.BILINEAR
            fill = None
            method = AutoAugment(policy, interpolation, fill)
            return method

        elif transform_type == 'RandAugment':
            num_ops = 2  # Number of augmentation transformations to apply sequentially
            magnitude = 9  # Magnitude for all the transformations
            num_magnitude_bins = 31  # The number of different magnitude values
            interpolation = InterpolationMode.BILINEAR
            fill = None
            method = RandAugment(num_ops, magnitude, num_magnitude_bins, interpolation, fill)
            return method

        elif transform_type == 'AugMix':
            severity = 3  # The severity of base augmentation operators
            mixture_width = 3  # The number of augmentation chains
            chain_depth = -1  # The depth of augmentation chains
            alpha = 1.0  # The hyperparameter for the probability distributions
            all_ops = True  # Use all operations (including brightness, contrast, color, and sharpness)
            interpolation = InterpolationMode.BILINEAR
            fill = None
            method = AugMix(severity, mixture_width, chain_depth, alpha, all_ops, interpolation, fill)
            return method
            
        method = getattr(transforms, transform_type)
        if transform_type=='RandomResizedCrop':
            return method(self.image_size) 
        elif transform_type=='CenterCrop':
            return method(self.image_size) 
        elif transform_type=='Resize':
            return method([int(self.image_size*1.15), int(self.image_size*1.15)])
        elif transform_type=='Normalize':
            return method(**self.normalize_param )

        else:
            return method()

    def get_composed_transform(self, aug=None, sn=False):
        if aug == 'standard' and sn:
            transform_list = ['RandomResizedCrop', 'StainNetTransform', 'RandomVerticalFlip', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']
        elif aug == 'standard':
            transform_list = ['RandomResizedCrop', 'RandomVerticalFlip', 'RandomHorizontalFlip', 'ToTensor', 'Normalize']
        elif aug == 'auto' and sn:
            transform_list = ['AutoAugment', 'StainNetTransform', 'ToTensor', 'Normalize']
        elif aug == 'auto':
            transform_list = ['AutoAugment', 'ToTensor', 'Normalize']
        elif aug == 'rand' and sn:
            transform_list = ['RandAugment', 'StainNetTransform', 'ToTensor', 'Normalize']
        elif aug == 'rand':
            transform_list = ['RandAugment', 'ToTensor', 'Normalize']
        elif aug == 'augmix' and sn:
            transform_list = ['AugMix', 'StainNetTransform', 'ToTensor', 'Normalize']
        elif aug == 'augmix':
            transform_list = ['AugMix', 'ToTensor', 'Normalize']
        elif (aug == 'none' or aug == 'cutmix' or aug =='mixup') and sn:
            transform_list = ['Resize', 'CenterCrop', 'StainNetTransform', 'ToTensor', 'Normalize']
        elif aug == 'none' or aug == 'cutmix' or aug =='mixup':
            transform_list = ['Resize', 'CenterCrop', 'ToTensor', 'Normalize']
        else:
            raise  ValueError(f"Unsupported augmentation: {aug}")

        transform_funcs = [ self.parse_transform(x) for x in transform_list]
        transform = transforms.Compose(transform_funcs)
        return transform



class DataManager:
    @abstractmethod
    def get_data_loader(self, data_file, aug, sn):
        pass 



class SimpleDataManager(DataManager):
    def __init__(self, image_size, batch_size):        
        super(SimpleDataManager, self).__init__()
        self.batch_size = batch_size
        self.trans_loader = TransformLoader(image_size)
        self.advanced_transform = None
    '''
    def collate_fn(self, batch):
        return self.advanced_transform(*default_collate(batch))
    '''
    
    def get_data_loader(self, data_file, aug, sn): #parameters that would change on train/val set
        '''
        if aug == 'cutmix':
          self.advanced_transform = v2.CutMix(num_classes=2)
        if aug == 'mixup':
          self.advanced_transform = v2.MixUp(num_classes=2)
        '''
  


        transform = self.trans_loader.get_composed_transform(aug = aug, sn=sn)
        dataset = SimpleDataset(data_file, transform = transform)

        #if aug == 'cutmix' or aug == 'mixup':
        #  data_loader_params = dict(batch_size = self.batch_size, shuffle = True, num_workers = os.cpu_count(), pin_memory = True, collate_fn=self.collate_fn)       
        #else:
        data_loader_params = dict(batch_size = self.batch_size, shuffle = True, num_workers = os.cpu_count(), pin_memory = True) 

        data_loader = torch.utils.data.DataLoader(dataset, **data_loader_params)

        return data_loader

class SetDataManager(DataManager):
    def __init__(self, image_size, n_way, n_support, n_query, n_eposide =100):        
        super(SetDataManager, self).__init__()
        self.image_size = image_size
        self.n_way = n_way
        self.batch_size = n_support + n_query
        self.n_eposide = n_eposide

        self.trans_loader = TransformLoader(image_size)

    def get_data_loader(self, data_file, aug, sn, cutmix = False, mixup = False): #parameters that would change on train/val set
        if aug == 'cutmix':
          advanced_transform = v2.CutMix(num_classes=self.n_way)
        if aug == 'mixup':
          advanced_transform = v2.MixUp(num_classes=self.n_way)


        transform = self.trans_loader.get_composed_transform(aug = aug, sn=sn)
        dataset = SetDataset( data_file , self.batch_size, transform = transform)
        sampler = EpisodicBatchSampler(len(dataset), self.n_way, self.n_eposide )  

        if aug == 'cutmix' or aug == 'mixup':
          data_loader_params = dict(batch_sampler = sampler,  num_workers = os.cpu_count(), pin_memory = True, collate_fn=collate_fn)       
        else:
          data_loader_params = dict(batch_sampler = sampler,  num_workers = os.cpu_count(), pin_memory = True)       
  
        data_loader = torch.utils.data.DataLoader(dataset, **data_loader_params)
        return data_loader
