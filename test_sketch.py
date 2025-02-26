import argparse
import logging
import os
import os.path as osp
import time

import cv2
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from basicsr.utils import (get_env_info, get_root_logger, get_time_str,
                           img2tensor, scandir, tensor2img)
from basicsr.utils.options import copy_opt_file, dict2str
from omegaconf import OmegaConf
from PIL import Image
from pytorch_lightning import seed_everything

from dataset_coco import dataset_coco, dataset_coco_mask_color_sig
from dist_util import get_bare_model, init_dist, master_only
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.dpm_solver import DPMSolverSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.modules.encoders.adapter import Adapter
from ldm.util import instantiate_from_config
from model_edge import pidinet


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    if "state_dict" in pl_sd:
        sd = pl_sd["state_dict"]
    else:
        sd = pl_sd
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.cuda()
    model.eval()
    return model

@master_only
def mkdir_and_rename(path):
    """mkdirs. If path exists, rename it with timestamp and create a new one.

    Args:
        path (str): Folder path.
    """
    if osp.exists(path):
        new_name = path + '_archived_' + get_time_str()
        print(f'Path already exists. Rename it to {new_name}', flush=True)
        os.rename(path, new_name)
    os.makedirs(path, exist_ok=True)
    os.makedirs(osp.join(experiments_root, 'models'))
    os.makedirs(osp.join(experiments_root, 'training_states'))
    os.makedirs(osp.join(experiments_root, 'visualization'))

def load_resume_state(opt):
    resume_state_path = None
    if opt.auto_resume:
        state_path = osp.join('experiments', opt.name, 'training_states')
        if osp.isdir(state_path):
            states = list(scandir(state_path, suffix='state', recursive=False, full_path=False))
            if len(states) != 0:
                states = [float(v.split('.state')[0]) for v in states]
                resume_state_path = osp.join(state_path, f'{max(states):.0f}.state')
                opt.resume_state_path = resume_state_path

    if resume_state_path is None:
        resume_state = None
    else:
        device_id = torch.cuda.current_device()
        resume_state = torch.load(resume_state_path, map_location=lambda storage, loc: storage.cuda(device_id))

    return resume_state

parser = argparse.ArgumentParser()
parser.add_argument(
        "--prompt",
        type=str,
        nargs="?",
        default="A car with flying wings"
    )
parser.add_argument(
        "--path_cond",
        type=str,
        default="examples/sketch/car.png"
)
parser.add_argument(
        "--type_in",
        type=str,
        default="sketch"
)
parser.add_argument(
    "--bsize",
    type=int,
    default=8,
    help="the prompt to render"
)
parser.add_argument(
    "--epochs",
    type=int,
    default=10000,
    help="the prompt to render"
)
parser.add_argument(
    "--num_workers",
    type=int,
    default=8,
    help="the prompt to render"
)
parser.add_argument(
    "--use_shuffle",
    type=bool,
    default=True,
    help="the prompt to render"
)
parser.add_argument(
        "--dpm_solver",
        action='store_true',
        help="use dpm_solver sampling",
)
parser.add_argument(
        "--plms",
        action='store_true',
        help="use plms sampling",
)
parser.add_argument(
        "--auto_resume",
        action='store_true',
        help="use plms sampling",
)
parser.add_argument(
        "--ckpt",
        type=str,
        default="models/sd-v1-4.ckpt",
        help="path to checkpoint of model",
)
parser.add_argument(
        "--ckpt_ad",
        type=str,
        default="models/t2iadapter_sketch_sd14v1.pth"
)
parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/test_sketch.yaml",
        help="path to config which constructs model",
)
parser.add_argument(
        "--print_fq",
        type=int,
        default=100,
        help="path to config which constructs model",
)
parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
)
parser.add_argument(
    "--W",
    type=int,
    default=512,
    help="image width, in pixel space",
)
parser.add_argument(
    "--C",
    type=int,
    default=4,
    help="latent channels",
)
parser.add_argument(
    "--f",
    type=int,
    default=8,
    help="downsampling factor",
)
parser.add_argument(
        "--ddim_steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
)
parser.add_argument(
        "--n_samples",
        type=int,
        default=10,
        help="how many samples to produce for each given prompt. A.k.a. batch size",
)
parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
)
parser.add_argument(
        "--scale",
        type=float,
        default=7.5,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
)
parser.add_argument(
        "--gpus",
        default=[0,1,2,3],
        help="gpu idx",
)
parser.add_argument(
        '--local_rank',
        default=-1,
        type=int,
        help='node rank for distributed training'
)
parser.add_argument(
        '--launcher',
        default='pytorch',
        type=str,
        help='node rank for distributed training'
)
opt = parser.parse_args()

if __name__ == '__main__':
    # seed_everything(42)
    config = OmegaConf.load(f"{opt.config}")
    opt.name = config['name']

    # distributed setting
    init_dist(opt.launcher)
    torch.backends.cudnn.benchmark = True
    device='cuda'

    # stable diffusion
    model = load_model_from_config(config, f"{opt.ckpt}").to(device)

    # Adaptor
    model_ad = Adapter(channels=[320, 640, 1280, 1280][:4], nums_rb=2, ksize=1, sk=True, use_conv=False).to(device)

    # edge_generator
    net_G = pidinet()
    ckp = torch.load('models/table5_pidinet.pth', map_location='cpu')['state_dict']
    net_G.load_state_dict({k.replace('module.',''):v for k, v in ckp.items()})
    net_G.cuda()

    # to gpus
    model_ad = torch.nn.parallel.DistributedDataParallel(
        model_ad,
        device_ids=[torch.cuda.current_device()])
    model_ad.module.load_state_dict(torch.load(opt.ckpt_ad))

    model = torch.nn.parallel.DistributedDataParallel(
        model,
        device_ids=[torch.cuda.current_device()])

    net_G = torch.nn.parallel.DistributedDataParallel(
        net_G,
        device_ids=[torch.cuda.current_device()])

    # root_path = osp.abspath(osp.join(__file__, osp.pardir, osp.pardir))
    experiments_root = osp.join('experiments', opt.name)

    # resume state
    resume_state = load_resume_state(opt)
    if resume_state is None:
        mkdir_and_rename(experiments_root)

    # copy the yml file to the experiment root
    copy_opt_file(opt.config, experiments_root)

    # WARNING: should not use get_root_logger in the above codes, including the called functions
    # Otherwise the logger will not be properly initialized
    log_file = osp.join(experiments_root, f"train_{opt.name}_{get_time_str()}.log")
    logger = get_root_logger(logger_name='basicsr', log_level=logging.INFO, log_file=log_file)
    logger.info(get_env_info())
    logger.info(dict2str(config))


    for v_idx in range(opt.n_samples):
        with torch.no_grad():
            if opt.dpm_solver:
                sampler = DPMSolverSampler(model.module)
            elif opt.plms:
                sampler = PLMSSampler(model.module)
            else:
                sampler = DDIMSampler(model.module)
            c = model.module.get_learned_conditioning([opt.prompt])

            if opt.type_in == 'sketch':
                # costumer input
                edge = cv2.imread(opt.path_cond)
                edge = cv2.resize(edge,(512,512))
                edge = img2tensor(edge)[0].unsqueeze(0).unsqueeze(0)/255.

                # edge = 1-edge # for white background
                edge = edge>0.5
                edge = edge.float()
            elif opt.type_in == 'image':
                im = cv2.imread(opt.path_cond)
                im = cv2.resize(im,(512,512))
                im = img2tensor(im).unsqueeze(0)/255.
                edge = net_G(im.cuda(non_blocking=True))[-1]

                edge = edge>0.5
                edge = edge.float()
            else:
                raise TypeError('Wrong input condition.')

            im_edge = tensor2img(edge)
            cv2.imwrite(os.path.join(experiments_root, 'visualization', 'edge_idx%04d.png'%(v_idx)), im_edge)

            features_adapter = model_ad(edge)

            shape = [opt.C, opt.H // opt.f, opt.W // opt.f]

            samples_ddim, intermediates = sampler.sample(S=opt.ddim_steps,
                                                conditioning=c,
                                                batch_size=1,
                                                shape=shape,
                                                verbose=False,
                                                unconditional_guidance_scale=opt.scale,
                                                unconditional_conditioning=model.module.get_learned_conditioning(["ugly, tiling, poorly drawn hands, poorly drawn feet, poorly drawn face, out of frame, extra limbs, disfigured, deformed, body out of frame, bad anatomy, watermark, signature, cut off, low contrast, underexposed, overexposed, bad art, beginner, amateur, distorted face"]),
                                                eta=opt.ddim_eta,
                                                x_T=None,
                                                features_adapter1=features_adapter,
                                                mode = 'sketch'
                                                )

            x_samples_ddim = model.module.decode_first_stage(samples_ddim)
            x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
            x_samples_ddim = x_samples_ddim.cpu().permute(0, 2, 3, 1).numpy()
            for id_sample, x_sample in enumerate(x_samples_ddim):
                x_sample = 255.*x_sample
                img = x_sample.astype(np.uint8)
                cv2.imwrite(os.path.join(experiments_root, 'visualization', 'sample_idx%04d_s%04d.png'%(v_idx, id_sample)), img[:,:,::-1])
