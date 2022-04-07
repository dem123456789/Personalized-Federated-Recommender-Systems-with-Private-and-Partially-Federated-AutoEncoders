import argparse
import copy
import datetime
from platform import node
from numpy import mod

from torch.optim import optimizer
import models
import os
import shutil
import time
import torch
import copy
import gc
import sys
import math
import random
import numpy as np
import collections
import torch.nn as nn
import torch.backends.cudnn as cudnn
from config import cfg, process_args
from data import fetch_dataset, make_data_loader, split_dataset, SplitDataset
from metrics import Metric
from fed import Federation
from utils import processed_folder, save, load, to_device, process_control, process_dataset, make_optimizer, make_scheduler, resume, collate, concatenate_path
from logger import make_logger

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
cudnn.benchmark = True
# create parser
parser = argparse.ArgumentParser(description='cfg')
# use add_argument() to add the value in yaml to parser
for k in cfg:
    exec('parser.add_argument(\'--{0}\', default=cfg[\'{0}\'], type=type(cfg[\'{0}\']))'.format(k))
# add a new key (--control_name) in parser, value is None
parser.add_argument('--control_name', default=None, type=str)
# vars() returns the dict object of the key:value (typed in by the user) of parser.parse_args(). args now is dict 
args = vars(parser.parse_args())
# Updata the cfg using args in helper function => config.py / process_args(args)
process_args(args)


def main():
    # utils.py / process_control()
    # disassemble cfg['control']
    # add the model parameter
    process_control()
    # Get all integer from cfg['init_seen'] to cfg['init_seed'] + cfg['num_experiments'] - 1
    seeds = list(range(cfg['init_seed'], cfg['init_seed'] + cfg['num_experiments']))
    for i in range(cfg['num_experiments']):
        # (seens[i] + cfg['control_name']) as experiment label
        model_tag_list = [str(seeds[i]), cfg['control_name']]
        cfg['model_tag'] = '_'.join([x for x in model_tag_list if x])
        print('Experiment: {}'.format(cfg['model_tag']))

        # Run experiment
        runExperiment()
    return

def runExperiment():

    # get seed and set the seed to CPU and GPU
    # same seed gives same result
    cfg['seed'] = int(cfg['model_tag'].split('_')[0])
    torch.manual_seed(cfg['seed'])
    torch.cuda.manual_seed(cfg['seed'])
    
    # data.py / fetch_dataset(ML100K)
    # dataset is a dict, has 2 keys - 'train', 'test'
    # dataset['train'] is the instance of corresponding dataset class
    # 一整个 =》 分开
    dataset = fetch_dataset(cfg['data_name'])

    # utils.py / process_dataset(dataset)
    # add some key:value (size, num) to cfg
    process_dataset(dataset)

    # resume

    # if data_split is None:
    data_split, data_split_info = split_dataset(dataset, cfg['num_nodes'], cfg['data_split_mode'])
    data_split['test'] = copy.deepcopy(data_split['train'])
    # data.py / make_data_loader(dataset)
    # data_loader is a dict, has 2 keys - 'train', 'test'
    # data_loader['train'] is the instance of DataLoader (class in PyTorch), which is iterable (可迭代对象)
    # data_loader = make_data_loader(dataset)

    # models / cfg['model_name'].py initializes the model, for example, models / ae.py / class AE
    # .to(cfg["device"]) means copy the tensor to the specific GPU or CPU, and run the 
    # calculation there.
    # model is the instance of class AE (in models / ae.py). It contains the training process of 
    #   Encoder and Decoder.
    federation = Federation(data_split_info)
    federation.create_local_model_and_local_optimizer()
    if cfg['compress_transmission'] == True:
        federation.record_global_grade_item_for_user(dataset['train'])

    if cfg['target_mode'] == 'explicit':
        # metric / class Metric
        # return the instance of Metric, which contains function and initial information
        #   we need for measuring the result
        metric = Metric({'train': ['Loss', 'RMSE'], 'test': ['Loss', 'RMSE']})
    elif cfg['target_mode'] == 'implicit':
        # metric / class Metric
        # return the instance of Metric, which contains function and initial information
        #   we need for measuring the result
        metric = Metric({'train': ['Loss', 'Accuracy'], 'test': ['Loss', 'Accuracy', 'MAP']})
    else:
        raise ValueError('Not valid target mode')
    
    model = eval('models.{}(encoder_num_users=10, encoder_num_items=10,' 
                'decoder_num_users=10, decoder_num_items=10)'.format(cfg['model_name']))
    global_optimizer = make_optimizer(model, cfg['model_name'])
    global_scheduler = make_scheduler(global_optimizer, cfg['model_name'])

    # Handle resuming the training situation
    cur_file_path = os.path.abspath(__file__)
    if cfg['resume_mode'] == 1:
        result = resume(cfg['model_tag'])
        last_epoch = result['epoch']
        if last_epoch > 1:
            federation.global_model.load_state_dict(result['model_state_dict'])
            if cfg['model_name'] != 'base':
                global_optimizer.load_state_dict(result['optimizer_state_dict'])
                global_scheduler.load_state_dict(result['scheduler_state_dict'])
            logger = result['logger']
        else:
            logger_path = '../output/runs/train_{}'.format(cfg['model_tag'])
            logger = make_logger(logger_path)
    else:
        last_epoch = 1
        # logger_path = concatenate_path([cur_file_path, '..', 'output', 'runs', 'train_{}'.format(cfg['model_tag'])])
        # logger_path = concatenate_path(['output', 'runs', 'train_{}'.format(cfg['model_tag'])])
        logger_path = '../output/runs/train_{}'.format(cfg['model_tag'])
        logger = make_logger(logger_path)

    
    # a = os.path.join()
    # Train and Test the model for cfg[cfg['model_name']]['num_epochs'] rounds
    for epoch in range(last_epoch, cfg[cfg['model_name']]['num_epochs'] + 1):
        logger.safe(True)
        
        global_optimizer_lr = global_optimizer.state_dict()['param_groups'][0]['lr']
        node_idx, total_item_union = train(dataset['train'], data_split['train'], data_split_info, federation, metric, logger, epoch, global_optimizer_lr)
        federation.update_global_model_momentum()
        model_state_dict = federation.global_model.state_dict()
        info = test(dataset['test'], data_split['train'], data_split_info, federation, metric, logger, epoch)

        # info = test_batchnorm(dataset['test'], data_split['test'], data_split_info, federation, metric, logger, epoch)
        global_scheduler.step()
        if cfg['experiment_size'] == 'large':
            logger.append_compress_item_union(total_item_union, epoch)
        logger.safe(False)

        result = {'cfg': cfg, 'epoch': epoch + 1, 'active_node_count': len(node_idx), 'info': info, 'logger': logger, 'model_state_dict': model_state_dict, 'data_split': data_split, 'data_split_info': data_split_info}
        # checkpoint_path = concatenate_path(['..', 'output', 'model', '{}_checkpoint.pt'.format(cfg['model_tag'])])
        # best_path = concatenate_path(['..', 'output', 'model', '{}_best.pt'.format(cfg['model_tag'])])
        if cfg['update_best_model'] == 'global':
            checkpoint_path = '../output/model/{}_checkpoint.pt'.format(cfg['model_tag'])
            best_path = '../output/model/{}_best.pt'.format(cfg['model_tag'])
            save(result, checkpoint_path)
            test_result = logger.mean['test/{}'.format(metric.pivot_name)]
            if metric.compare(test_result):
                metric.update(test_result)
                shutil.copy(checkpoint_path, best_path)
        elif cfg['update_best_model'] == 'local': 
            checkpoint_path = '../output/model/{}/checkpoint.pt'.format(cfg['model_tag'])         
            save(result, checkpoint_path)

            test_result = logger.mean_for_each_node['test/{}'.format(metric.pivot_name)]
            update_index_list = metric.compare(test_result)
            metric.update(test_result, update_index_list)
            for node_idx in range(len(update_index_list)):
                if update_index_list[node_idx] == True:
                    best_path = '../output/model/{}/{}.pt'.format(cfg['model_tag'], node_idx)
                    save(federation.load_local_model(node_idx), best_path)
        else:
            raise ValueError('Not valid update_best_model way')

        logger.reset()

    return



def train(dataset, data_split, data_split_info, federation, metric, logger, epoch, global_optimizer_lr):

    """
    train the model

    Parameters:
        data_loader - Object. Instance of DataLoader(data.py / make_data_loader(dataset)). 
            It constains the processed data for training. data_loader['train'] is the instance of DataLoader (class in PyTorch), 
            which is iterable (可迭代对象)
        model - Object. Instance of class AE (in models / ae.py). 
            It contains the training process of Encoder and Decoder.
        optimizer - Object. Instance of class Optimizer, which is in Pytorch(utils.py / make_optimizer()). 
            It contains the method to adjust learning rate.
        metric - Object. Instance of class Metric (metric / class Metric).
            It contains function and initial information we need for measuring the result
        logger - Object. Instance of logger.py / class Logger.
        epoch - Integer. The epoch number in for loop.

    Returns:
        None

    Raises:
        None
    """

    local, node_idx = make_local(dataset, data_split, data_split_info, federation, metric)
    start_time = time.time()

    total_item_union = 0
    for m in range(len(node_idx)):
        item_union_set = None
        if cfg['compress_transmission'] == True:
            item_union_set = federation.calculate_item_union_set(node_idx[m], data_split[node_idx[m]])
            total_item_union += len(item_union_set)
        federation.generate_new_global_model_parameter_dict(local[m].train(logger, federation, node_idx[m], global_optimizer_lr), len(node_idx), item_union_set)

        
        if m % int((len(node_idx) * cfg['log_interval']) + 1) == 0:
            local_time = (time.time() - start_time) / (m + 1)
            epoch_finished_time = datetime.timedelta(seconds=local_time * (len(node_idx) - m - 1))
            # exp_finished_time = epoch_finished_time + datetime.timedelta(
            #     seconds=round((cfg['num_epochs']['global'] - epoch) * local_time * num_active_nodes))
            exp_finished_time = 1
            info = {'info': ['Model: {}'.format(cfg['model_tag']), 
                             'Train Epoch: {}({:.0f}%)'.format(epoch, 100. * m / len(node_idx)),
                             'ID: {}({}/{})'.format(node_idx[m], m + 1, len(node_idx)),
                             'Epoch Finished Time: {}'.format(epoch_finished_time),
                             'Experiment Finished Time: {}'.format(exp_finished_time)]}
            logger.append(info, 'train', mean=False)
            print(logger.write('train', metric.metric_name['train']))

    return node_idx, total_item_union


def test(dataset, data_split, data_split_info, federation, metric, logger, epoch):

    with torch.no_grad():
        for m in range(len(data_split)):
            user_per_node_i = data_split_info[m]['num_users']
            batch_size = {'test': min(user_per_node_i, cfg[cfg['model_name']]['batch_size']['test'])}
            data_loader = make_data_loader({'test': SplitDataset(dataset, data_split[m])}, batch_size)['test']

            model = federation.load_local_model(m)
            model.to(cfg['device'])
            model = federation.update_client_parameters_with_global_parameters(model)
            
            model.train(False)
            for i, original_input in enumerate(data_loader):
                input = copy.deepcopy(original_input)
                input = collate(input)
                input_size = len(input['target_{}'.format(cfg['data_mode'])])
                if input_size == 0:
                    continue
                input = to_device(input, cfg['device'])
                output = model(input)
                
                if cfg['experiment_size'] == 'large':
                    input = to_device(input, 'cpu')
                    output = to_device(output, 'cpu')

                if cfg['update_best_model'] == 'global':
                    evaluation = metric.evaluate(metric.metric_name['test'], input, output)
                    logger.append(evaluation, 'test', input_size)
                elif cfg['update_best_model'] == 'local':
                    evaluation = metric.evaluate(metric.metric_name['test'], input, output, m)
                    logger.append(evaluation, 'test', input_size)

            if cfg['experiment_size'] == 'large':
                model.to('cpu')
        info = {'info': ['Model: {}'.format(cfg['model_tag']),
                         'Test Epoch: {}({:.0f}%)'.format(epoch, 100.)]}
        logger.append(info, 'test', mean=False)
        info = logger.write('test', metric.metric_name['test'])
        print(info)

    return info

def make_local(dataset, data_split, data_split_info, federation, metric):
    num_active_nodes = int(np.ceil(cfg[cfg['model_name']]['fraction'] * cfg['num_nodes']))
    node_idx = torch.arange(cfg['num_nodes'])[torch.randperm(cfg['num_nodes'])[:num_active_nodes]].tolist()
    local = [None for _ in range(num_active_nodes)]

    for m in range(num_active_nodes):
        cur_node_index = node_idx[m]
        user_per_node_i = data_split_info[cur_node_index]['num_users']

        batch_size = {'train': min(user_per_node_i, cfg[cfg['model_name']]['batch_size']['train'])}
        data_loader_m = make_data_loader({'train': SplitDataset(dataset, 
            data_split[cur_node_index])}, batch_size)['train']

        cur_local_model = federation.load_local_model(cur_node_index)
        cur_local_model = federation.update_client_parameters_with_global_parameters(cur_local_model)
        local[m] = Local(data_loader_m, cur_local_model, metric)
    return local, node_idx


class Local:
    def __init__(self, data_loader, local_model, metric):
        self.data_loader = data_loader
        self.local_model = local_model
        self.metric = metric

    def train(self, logger, federation, cur_node_index, global_optimizer_lr):

        model = self.local_model
        model.to(cfg['device'])
        model.train(True)

        optimizer = make_optimizer(model, cfg['model_name'])      
        local_optimizer_state_dict = federation.get_local_optimizer_state_dict(cur_node_index) 
        local_optimizer_state_dict = to_device(local_optimizer_state_dict, cfg['device'])
        optimizer.load_state_dict(local_optimizer_state_dict) 
        optimizer.param_groups[0]['lr'] = global_optimizer_lr

        for local_epoch in range(1, cfg[cfg['model_name']]['local_epoch'] + 1):
            for i, original_input in enumerate(self.data_loader):
                input = copy.deepcopy(original_input)
                input = collate(input)
                input_size = len(input['target_{}'.format(cfg['data_mode'])])
                if input_size == 0:
                    continue
                input = to_device(input, cfg['device'])
                output = model(input)
                
                if optimizer is not None:
                    # Zero the gradient
                    optimizer.zero_grad()
                    # Calculate the gradient of each parameter
                    output['loss'].backward()
                    # Clips gradient norm of an iterable of parameters.
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
                    # Perform a step of parameter through gradient descent Update
                    optimizer.step()

                if cfg['experiment_size'] == 'large':
                    input = to_device(input, 'cpu')
                    output = to_device(output, 'cpu')

                evaluation = self.metric.evaluate(self.metric.metric_name['train'], input, output)
                logger.append(evaluation, 'train', n=input_size)
        
        if cfg['experiment_size'] == 'large':
            model.to('cpu')
            optimizer_state_dict = optimizer.state_dict()
            optimizer_state_dict = to_device(optimizer_state_dict, 'cpu')

        federation.store_local_model(cur_node_index, model)
        federation.store_local_optimizer_state_dict(cur_node_index, copy.deepcopy(optimizer_state_dict))
        
        # b = next(model.parameters()).device
        local_parameters = model.state_dict()

        return local_parameters


if __name__ == "__main__":
    main()
