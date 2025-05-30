import glob
import os
import wandb

import torch
import tqdm
import time
import contextlib

from torch.nn.utils import clip_grad_norm_
from pcdet.utils import common_utils, commu_utils

try:
    import torch.cuda.amp
except:
    # Make sure the torch version is latest enough to support mixed precision training
    pass




def train_one_epoch(model, optimizer, train_loader, model_func, lr_scheduler, accumulated_iter, optim_cfg,
                    rank, tbar, total_it_each_epoch, dataloader_iter, tb_log=None, leave_pbar=False,
                    use_logger_to_record=False, logger=None, logger_iter_interval=50, cur_epoch=None,
                    total_epochs=None, ckpt_save_dir=None, ckpt_save_time_interval=300, show_gpu_stat=False, fp16=False, run=None):
    if total_it_each_epoch == len(train_loader):
        dataloader_iter = iter(train_loader)

    ckpt_save_cnt = 1
    start_it = accumulated_iter % total_it_each_epoch

    if rank == 0:
        pbar = tqdm.tqdm(total=total_it_each_epoch, leave=leave_pbar, desc='train', dynamic_ncols=True)
        data_time = common_utils.AverageMeter()
        batch_time = common_utils.AverageMeter()
        forward_time = common_utils.AverageMeter()
        loss_disp = common_utils.AverageMeter()
        # just for centerhead
        hm_loss_disp = common_utils.AverageMeter()
        loc_loss_disp = common_utils.AverageMeter()
        rcnn_cls_loss_disp = common_utils.AverageMeter()
        rcnn_reg_loss_disp = common_utils.AverageMeter()


    amp_ctx = contextlib.nullcontext()
    if fp16:
        scaler = torch.cuda.amp.grad_scaler.GradScaler(init_scale=optim_cfg.get('LOSS_SCALE_FP16', 2.0**16))
        amp_ctx = torch.cuda.amp.autocast()


    end = time.time()
    for cur_it in range(start_it, total_it_each_epoch):
        try:
            batch = next(dataloader_iter)
        except StopIteration:
            dataloader_iter = iter(train_loader)
            batch = next(dataloader_iter)
            print('new iters')

        data_timer = time.time()
        cur_data_time = data_timer - end

        lr_scheduler.step(accumulated_iter)

        try:
            cur_lr = float(optimizer.lr)
        except:
            cur_lr = optimizer.param_groups[0]['lr']

        if tb_log is not None:
            tb_log.add_scalar('meta_data/learning_rate', cur_lr, accumulated_iter)

        model.train()
        optimizer.zero_grad()

        with amp_ctx:
            loss, tb_dict, disp_dict = model_func(model, batch)
            if fp16:
                assert loss.dtype is torch.float32
                scaler.scale(loss).backward()
                # unscale gradient for clip gradient
                scaler.unscale_(optimizer)
                total_norm = clip_grad_norm_(model.parameters(), optim_cfg.GRAD_NORM_CLIP)
                scaler.step(optimizer)
                scaler.update()
            else:
                loss.backward()
                total_norm = clip_grad_norm_(model.parameters(), optim_cfg.GRAD_NORM_CLIP)
                optimizer.step()

        accumulated_iter += 1
        # assert not torch.isnan(loss)

        cur_forward_time = time.time() - data_timer
        cur_batch_time = time.time() - end
        end = time.time()

        # average reduce
        avg_data_time = commu_utils.average_reduce_value(cur_data_time)
        avg_forward_time = commu_utils.average_reduce_value(cur_forward_time)
        avg_batch_time = commu_utils.average_reduce_value(cur_batch_time)

        if rank == 0:
            # log to console and tensorboard
            run.log({"loss/train": loss.item(), "learning_rate": cur_lr}, accumulated_iter)
            data_time.update(avg_data_time)
            forward_time.update(avg_forward_time)
            batch_time.update(avg_batch_time)
            loss_disp.update(loss.item())
            
            # for centerhead
            if 'hm_loss_head_0' in list(tb_dict.keys()) and 'loc_loss_head_0' in list(tb_dict.keys()):
                hm_loss_disp.update(tb_dict['hm_loss_head_0'])
                loc_loss_disp.update(tb_dict['loc_loss_head_0'])
                disp_dict.update({
                'loss_hm': f'{hm_loss_disp.avg:.4f}', 'loss_loc': f'{loc_loss_disp.avg:.4f}'})
            if 'rcnn_loss_reg' in list(tb_dict.keys()) and 'rcnn_loss_cls' in list(tb_dict.keys()):
                rcnn_cls_loss_disp.update(tb_dict['rcnn_loss_cls'])
                rcnn_reg_loss_disp.update(tb_dict['rcnn_loss_reg'])
                disp_dict.update({
                'loss_rcnn_cls': f'{rcnn_cls_loss_disp.avg:.4f}', 'loss_rcnn_reg': f'{rcnn_reg_loss_disp.avg:.4f}'})
            disp_dict.update({
                'loss': loss_disp.avg, 'lr': cur_lr, 'd_time': f'{data_time.val:.2f}({data_time.avg:.2f})',
                'f_time': f'{forward_time.val:.2f}({forward_time.avg:.2f})', 'b_time': f'{batch_time.val:.2f}({batch_time.avg:.2f})',
                'norm': total_norm.item()
            })

            if use_logger_to_record:
                if (accumulated_iter % logger_iter_interval == 0 and cur_it != start_it) or cur_it + 1 == total_it_each_epoch:
                    trained_time_past_all = tbar.format_dict['elapsed']
                    second_each_iter = pbar.format_dict['elapsed'] / max(cur_it - start_it + 1, 1.0)

                    trained_time_each_epoch = pbar.format_dict['elapsed']
                    remaining_second_each_epoch = second_each_iter * (total_it_each_epoch - cur_it)
                    remaining_second_all = second_each_iter * ((total_epochs - cur_epoch) * total_it_each_epoch - cur_it)

                    disp_str = ', '.join([f'{key}={val}' for key, val in disp_dict.items() if key != 'lr'])
                    disp_str += f', lr={disp_dict["lr"]}'
                    batch_size = batch.get('batch_size', None)
                    logger.info(f'epoch: {cur_epoch}/{total_epochs}, acc_iter={accumulated_iter}, cur_iter={cur_it}/{total_it_each_epoch}, batch_size={batch_size}, '
                                f'time_cost(epoch): {tbar.format_interval(trained_time_each_epoch)}/{tbar.format_interval(remaining_second_each_epoch)}, '
                                f'time_cost(all): {tbar.format_interval(trained_time_past_all)}/{tbar.format_interval(remaining_second_all)}, '
                                f'{disp_str}')
                    if show_gpu_stat and accumulated_iter % (3 * logger_iter_interval) == 0:
                        # To show the GPU utilization, please install gpustat through "pip install gpustat"
                        gpu_info = os.popen('gpustat').read()
                        logger.info(gpu_info)
                    
                    loss_disp.reset()  # WHY
                    hm_loss_disp.reset()
                    loc_loss_disp.reset()
                    rcnn_cls_loss_disp.reset()
                    rcnn_reg_loss_disp.reset()
            else:
                pbar.update()
                pbar.set_postfix(dict(total_it=accumulated_iter))
                tbar.set_postfix(disp_dict)
                # tbar.refresh()

            if tb_log is not None:
                tb_log.add_scalar('train/loss', loss, accumulated_iter)
                tb_log.add_scalar('meta_data/learning_rate', cur_lr, accumulated_iter)
                for key, val in tb_dict.items():
                    tb_log.add_scalar('train/' + key, val, accumulated_iter)

            # save intermediate ckpt every {ckpt_save_time_interval} seconds
            time_past_this_epoch = pbar.format_dict['elapsed']
            if time_past_this_epoch // ckpt_save_time_interval >= ckpt_save_cnt:
                ckpt_name = ckpt_save_dir / 'latest_model'
                save_checkpoint(
                    checkpoint_state(model, optimizer, cur_epoch, accumulated_iter), filename=ckpt_name,
                )
                logger.info(f'Save latest model to {ckpt_name}')
                ckpt_save_cnt += 1

    if rank == 0:
        pbar.close()
    return accumulated_iter


def train_model(model, optimizer, train_loader, model_func, lr_scheduler, optim_cfg,
                start_epoch, total_epochs, start_iter, rank, tb_log, ckpt_save_dir, train_sampler=None,
                lr_warmup_scheduler=None, ckpt_save_interval=1, max_ckpt_save_num=50,
                merge_all_iters_to_one_epoch=False,
                use_logger_to_record=False, logger=None, logger_iter_interval=None, ckpt_save_time_interval=None, show_gpu_stat=False, fp16=False, cfg=None):
    accumulated_iter = start_iter
    if rank == 0:
        wandb.login(key="96ef26e86e1f7cf07e5546dda5a50e78ad102bcf")
        run = wandb.init(project="DSVT")
        run.config.update(optim_cfg)
    else:
        run = None
    
    

    augment_disable_flag = False
    with tqdm.trange(start_epoch, total_epochs, desc='epochs', dynamic_ncols=True, leave=(rank == 0)) as tbar:
        total_it_each_epoch = len(train_loader)
        if merge_all_iters_to_one_epoch:
            assert hasattr(train_loader.dataset, 'merge_all_iters_to_one_epoch')
            train_loader.dataset.merge_all_iters_to_one_epoch(merge=True, epochs=total_epochs)
            total_it_each_epoch = len(train_loader) // max(total_epochs, 1)

        dataloader_iter = iter(train_loader)
        for cur_epoch in tbar:
            if train_sampler is not None:
                train_sampler.set_epoch(cur_epoch)

            # train one epoch
            if lr_warmup_scheduler is not None and cur_epoch < optim_cfg.WARMUP_EPOCH:
                cur_scheduler = lr_warmup_scheduler
            else:
                cur_scheduler = lr_scheduler
            
            hook_config = cfg.get('HOOK', None) 
            if hook_config is not None:
                DisableAugmentationHook = hook_config.get('DisableAugmentationHook', None)
                if DisableAugmentationHook is not None:
                    num_last_epochs = cfg.HOOK.DisableAugmentationHook.NUM_LAST_EPOCHS
                    if (total_epochs - num_last_epochs) <= cur_epoch and not augment_disable_flag:
                        from pcdet.datasets.augmentor.data_augmentor import DataAugmentor
                        from pathlib import Path
                        DISABLE_AUG_LIST = cfg.HOOK.DisableAugmentationHook.DISABLE_AUG_LIST
                        dataset_cfg=cfg.DATA_CONFIG
                        # This hook turns off some data augmentation strategies. 
                        logger.info(f'Disable augmentations: {DISABLE_AUG_LIST}')
                        dataset_cfg.DATA_AUGMENTOR.DISABLE_AUG_LIST = DISABLE_AUG_LIST
                        class_names=cfg.CLASS_NAMES
                        root_path = Path(dataset_cfg.DATA_PATH)
                        new_data_augmentor = DataAugmentor(root_path, dataset_cfg.DATA_AUGMENTOR, class_names, logger=logger)
                        dataloader_iter._dataset.data_augmentor = new_data_augmentor
                        augment_disable_flag = True


            accumulated_iter = train_one_epoch(
                model, optimizer, train_loader, model_func,
                lr_scheduler=cur_scheduler,
                accumulated_iter=accumulated_iter, optim_cfg=optim_cfg,
                rank=rank, tbar=tbar, tb_log=tb_log,
                leave_pbar=(cur_epoch + 1 == total_epochs),
                total_it_each_epoch=total_it_each_epoch,
                dataloader_iter=dataloader_iter,

                cur_epoch=cur_epoch, total_epochs=total_epochs,
                use_logger_to_record=use_logger_to_record,
                logger=logger, logger_iter_interval=logger_iter_interval,
                ckpt_save_dir=ckpt_save_dir, ckpt_save_time_interval=ckpt_save_time_interval,
                show_gpu_stat=show_gpu_stat,
                fp16=fp16,
                run=run
            )

            # save trained model
            trained_epoch = cur_epoch + 1
            if trained_epoch % ckpt_save_interval == 0 and rank == 0:

                ckpt_list = glob.glob(str(ckpt_save_dir / 'checkpoint_epoch_*.pth'))
                ckpt_list.sort(key=os.path.getmtime)

                if ckpt_list.__len__() >= max_ckpt_save_num:
                    for cur_file_idx in range(0, len(ckpt_list) - max_ckpt_save_num + 1):
                        os.remove(ckpt_list[cur_file_idx])

                ckpt_name = ckpt_save_dir / ('checkpoint_epoch_%d' % trained_epoch)
                save_checkpoint(
                    checkpoint_state(model, optimizer, trained_epoch, accumulated_iter), filename=ckpt_name,
                )


def model_state_to_cpu(model_state):
    model_state_cpu = type(model_state)()  # ordered dict
    for key, val in model_state.items():
        model_state_cpu[key] = val.cpu()
    return model_state_cpu


def checkpoint_state(model=None, optimizer=None, epoch=None, it=None):
    optim_state = optimizer.state_dict() if optimizer is not None else None
    if model is not None:
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model_state = model_state_to_cpu(model.module.state_dict())
        else:
            model_state = model.state_dict()
    else:
        model_state = None

    try:
        import pcdet
        version = 'pcdet+' + pcdet.__version__
    except:
        version = 'none'

    return {'epoch': epoch, 'it': it, 'model_state': model_state, 'optimizer_state': optim_state, 'version': version}


def save_checkpoint(state, filename='checkpoint'):
    if False and 'optimizer_state' in state:
        optimizer_state = state['optimizer_state']
        state.pop('optimizer_state', None)
        optimizer_filename = '{}_optim.pth'.format(filename)
        if torch.__version__ >= '1.4':
            torch.save({'optimizer_state': optimizer_state}, optimizer_filename, _use_new_zipfile_serialization=False)
        else:
            torch.save({'optimizer_state': optimizer_state}, optimizer_filename)

    filename = '{}.pth'.format(filename)
    if torch.__version__ >= '1.4':
        torch.save(state, filename, _use_new_zipfile_serialization=False)
    else:
        torch.save(state, filename)
