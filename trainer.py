from torchvision.transforms import InterpolationMode

from rgen import DEFAULT_CONFIG_PATH, PROJECT_ROOT
from . import dataloader as DL
from .config import config
from . import network as net
from math import floor, ceil
from datetime import datetime
import os, sys
import re
# os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"

import torch
import torchvision.transforms as transforms
from torch.autograd import Variable
from torch.optim import Adam
from tqdm import tqdm
# import tf_recorder as tensorboard
from . import utils as utils
import numpy as np


# import tensorflow as tf

class trainer:
    def __init__(self, config):
        self.config = config
        if torch.cuda.is_available():
            self.use_cuda = True
            torch.set_default_tensor_type('torch.cuda.FloatTensor')
        else:
            self.use_cuda = False
            torch.set_default_tensor_type('torch.FloatTensor')

        self.nz = config.nz
        self.optimizer = config.optimizer

        self.resl = 2  # we start from 2^2 = 4
        self.lr = config.lr
        self.eps_drift = config.eps_drift
        self.smoothing = config.smoothing
        self.max_resl = config.max_resl
        self.trns_tick = config.trns_tick
        self.stab_tick = config.stab_tick
        self.TICK = config.TICK
        self.globalIter = 0
        self.globalTick = 0
        self.kimgs = 0
        self.stack = 0
        self.epoch = 0
        self.fadein = {'gen': None, 'dis': None}
        self.complete = {'gen': 0, 'dis': 0}
        self.phase = 'init'
        self.flag_flush_gen = False
        self.flag_flush_dis = False
        self.flag_add_noise = self.config.flag_add_noise
        self.flag_add_drift = self.config.flag_add_drift

        # network and cirterion
        self.G = net.Generator(config)
        self.D = net.Discriminator(config)
        print('Generator structure: ')
        print(self.G.model)
        print('Discriminator structure: ')
        print(self.D.model)
        self.mse = torch.nn.MSELoss()
        if self.use_cuda:
            self.mse = self.mse.cuda()
            torch.cuda.manual_seed(config.random_seed)
            if config.n_gpu == 1:
                self.G = torch.nn.DataParallel(self.G).cuda(device=0)
                self.D = torch.nn.DataParallel(self.D).cuda(device=0)
            else:
                gpus = []
                for i in range(config.n_gpu):
                    gpus.append(i)
                self.G = torch.nn.DataParallel(self.G, device_ids=gpus).cuda()
                self.D = torch.nn.DataParallel(self.D, device_ids=gpus).cuda()

        # Load discriminator & generator checkpoints
        if config.resume_training_D and config.resume_training_G:
            resume_training = True
        resume_path_D = PROJECT_ROOT.joinpath(config.resume_training_D)
        resume_path_G = PROJECT_ROOT.joinpath(config.resume_training_G)
        if resume_training and os.path.exists(resume_path_D) and os.path.exists(resume_path_G):
            D_checkpoint = torch.load(resume_path_D)
            G_checkpoint = torch.load(resume_path_G)
            # Grow network according to checkpoint resl
            new_resl = D_checkpoint['resl']
            for resl in range(3, floor(new_resl) + 1):
                self.lr = self.lr * float(self.config.lr_decay)
                self.G.module.flush_network()
                self.D.module.flush_network()
                self.G.module.grow_network(resl)
                self.D.module.grow_network(resl)
            self.resl = new_resl

        # define tensors, ship model to cuda, and get dataloader.
        self.renew_everything()

        # tensorboard
        self.use_tb = False
        '''if self.use_tb:
            self.tb = tensorboard.tf_recorder()'''

        self.lr = D_checkpoint['learning_rate']

        # Load checkpoint
        if resume_training and os.path.exists(resume_path_D) and os.path.exists(resume_path_G):
            self.opt_d.load_state_dict(D_checkpoint['optimizer'])
            if not len(D_checkpoint['state_dict']) == len(self.D.module.state_dict()):
                self.D.module.flush_network()
            self.D.module.load_state_dict(D_checkpoint['state_dict'])
            self.opt_g.load_state_dict(G_checkpoint['optimizer'])
            if not len(G_checkpoint['state_dict']) == len(self.G.module.state_dict()):
                self.G.module.flush_network()
            self.G.module.load_state_dict(G_checkpoint['state_dict'])
            # match = re.search(r"R\d_T(\d*).pth.tar", "pggan/repo/model/dis_R6_T2400.pth.tar")
            # self.globalTick = int(match(1))
            self.globalTick = D_checkpoint['globalTick']
            self.globalIter = D_checkpoint['globalIter']
            self.phase = D_checkpoint['phase']
            self.stack = D_checkpoint['stack']
            self.epoch = D_checkpoint['epoch']
            self.complete['dis'] = D_checkpoint['complete']
            self.complete['gen'] = G_checkpoint['complete']
            self.flag_flush_dis = D_checkpoint['flush']
            self.flag_flush_gen = G_checkpoint['flush']

            self.renew_everything()

    def resl_scheduler(self):
        '''
        this function will schedule image resolution(self.resl) progressively.
        it should be called every iteration to ensure resl value is updated properly.
        step 1. (trns_tick) --> transition in generator.
        step 2. (stab_tick) --> stabilize.
        step 3. (trns_tick) --> transition in discriminator.
        step 4. (stab_tick) --> stabilize.
        '''
        if floor(self.resl) != 2:
            self.trns_tick = self.config.trns_tick
            self.stab_tick = self.config.stab_tick

        self.batchsize = self.loader.batchsize
        delta = 1.0 / (2 * self.trns_tick + 2 * self.stab_tick)
        d_alpha = 1.0 * self.batchsize / self.trns_tick / self.TICK

        # update alpha if fade-in layer exist.
        if self.fadein['gen'] is not None:
            if self.resl % 1.0 < (self.trns_tick) * delta:
                self.fadein['gen'].update_alpha(d_alpha)
                self.complete['gen'] = self.fadein['gen'].alpha * 100
                self.phase = 'gtrns'
            elif self.resl % 1.0 >= (self.trns_tick) * delta and self.resl % 1.0 < (
                    self.trns_tick + self.stab_tick) * delta:
                self.phase = 'gstab'
        if self.fadein['dis'] is not None:
            if self.resl % 1.0 >= (self.trns_tick + self.stab_tick) * delta and self.resl % 1.0 < (
                    self.stab_tick + self.trns_tick * 2) * delta:
                self.fadein['dis'].update_alpha(d_alpha)
                self.complete['dis'] = self.fadein['dis'].alpha * 100
                self.phase = 'dtrns'
            elif self.resl % 1.0 >= (self.stab_tick + self.trns_tick * 2) * delta and self.phase != 'final':
                self.phase = 'dstab'

        prev_kimgs = self.kimgs
        self.kimgs = self.kimgs + self.batchsize
        if (self.kimgs % self.TICK) < (prev_kimgs % self.TICK):
            self.globalTick = self.globalTick + 1
            # increase linearly every tick, and grow network structure.
            prev_resl = floor(self.resl)
            self.resl = self.resl + delta
            self.resl = max(2, min(10.5, self.resl))  # clamping, range: 4 ~ 1024

            # flush network.
            if self.flag_flush_gen and self.resl % 1.0 >= (self.trns_tick + self.stab_tick) * delta and prev_resl != 2:
                if self.fadein['gen'] is not None:
                    self.fadein['gen'].update_alpha(d_alpha)
                    self.complete['gen'] = self.fadein['gen'].alpha * 100
                self.flag_flush_gen = False
                self.G.module.flush_network()  # flush G
                print(self.G.module.model)
                # self.Gs.module.flush_network()         # flush Gs
                self.fadein['gen'] = None
                self.complete['gen'] = 0.0
                self.phase = 'dtrns'
            elif self.flag_flush_dis and floor(self.resl) != prev_resl and prev_resl != 2:
                if self.fadein['dis'] is not None:
                    self.fadein['dis'].update_alpha(d_alpha)
                    self.complete['dis'] = self.fadein['dis'].alpha * 100
                self.flag_flush_dis = False
                self.D.module.flush_network()  # flush and,
                print(self.D.module.model)
                self.fadein['dis'] = None
                self.complete['dis'] = 0.0
                if floor(self.resl) < self.max_resl and self.phase != 'final':
                    self.phase = 'gtrns'

            # grow network.
            if floor(self.resl) != prev_resl and floor(self.resl) < self.max_resl + 1:
                self.lr = self.lr * float(self.config.lr_decay)
                self.G.module.grow_network(floor(self.resl))
                # self.Gs.grow_network(floor(self.resl))
                self.D.module.grow_network(floor(self.resl))
                self.renew_everything()
                self.fadein['gen'] = dict(self.G.module.model.named_children())['fadein_block']
                self.fadein['dis'] = dict(self.D.module.model.named_children())['fadein_block']
                self.flag_flush_gen = True
                self.flag_flush_dis = True

            if floor(self.resl) >= self.max_resl and self.resl % 1.0 >= (self.stab_tick + self.trns_tick * 2) * delta:
                self.phase = 'final'
                self.resl = self.max_resl + (self.stab_tick + self.trns_tick * 2) * delta

    def renew_everything(self):
        # renew dataloader.
        self.loader = DL.dataloader(config)
        self.loader.renew(min(floor(self.resl), self.max_resl))

        # define tensors
        self.z = torch.FloatTensor(self.loader.batchsize, self.nz)
        self.x = torch.FloatTensor(self.loader.batchsize, 3, self.loader.imsize, self.loader.imsize)
        self.x_tilde = torch.FloatTensor(self.loader.batchsize, 3, self.loader.imsize, self.loader.imsize)
        self.real_label = torch.FloatTensor(self.loader.batchsize).fill_(1)
        self.fake_label = torch.FloatTensor(self.loader.batchsize).fill_(0)

        # enable cuda
        if self.use_cuda:
            self.z = self.z.cuda()
            self.x = self.x.cuda()
            self.x_tilde = self.x.cuda()
            self.real_label = self.real_label.cuda()
            self.fake_label = self.fake_label.cuda()
            torch.cuda.manual_seed(config.random_seed)

        # wrapping autograd Variable.
        self.x = Variable(self.x)
        self.x_tilde = Variable(self.x_tilde)
        self.z = Variable(self.z)
        self.real_label = Variable(self.real_label)
        self.fake_label = Variable(self.fake_label)

        # ship new model to cuda.
        if self.use_cuda:
            self.G = self.G.cuda()
            self.D = self.D.cuda()

        # optimizer
        betas = (self.config.beta1, self.config.beta2)
        if self.optimizer == 'adam':
            self.opt_g = Adam(filter(lambda p: p.requires_grad, self.G.parameters()), lr=self.lr, betas=betas,
                              weight_decay=0.0)
            self.opt_d = Adam(filter(lambda p: p.requires_grad, self.D.parameters()), lr=self.lr, betas=betas,
                              weight_decay=0.0)

    def feed_interpolated_input(self, x):
        if self.phase == 'gtrns' and floor(self.resl) > 2 and floor(self.resl) <= self.max_resl:
            alpha = self.complete['gen'] / 100.0
            transform = transforms.Compose([transforms.ToPILImage(),
                                            transforms.Resize(size=int(pow(2, floor(self.resl) - 1)),
                                                              interpolation=InterpolationMode.NEAREST),
                                            transforms.Resize(size=int(pow(2, floor(self.resl))),
                                                              interpolation=InterpolationMode.NEAREST),
                                            transforms.ToTensor(),
                                            ])
            x_low = x.clone().add(1).mul(0.5)
            for i in range(x_low.size(0)):
                x_low[i] = transform(x_low[i]).mul(2).add(-1)
            x = torch.add(x.mul(alpha), x_low.mul(1 - alpha))  # interpolated_x

        if self.use_cuda:
            return x.cuda()
        else:
            return x

    def add_noise(self, x):
        # TODO: support more method of adding noise.
        if self.flag_add_noise == False:
            return x

        if hasattr(self, '_d_'):
            self._d_ = self._d_ * 0.9 + torch.mean(self.fx_tilde).item() * 0.1
        else:
            self._d_ = 0.0
        strength = 0.2 * max(0, self._d_ - 0.5) ** 2
        z = np.random.randn(*x.size()).astype(np.float32) * strength
        z = Variable(torch.from_numpy(z)).cuda() if self.use_cuda else Variable(torch.from_numpy(z))
        return x + z

    def train(self):
        # noise for test.
        self.z_test = torch.FloatTensor(16, self.nz)
        if self.use_cuda:
            self.z_test = self.z_test.cuda()
        self.z_test = Variable(self.z_test, volatile=True)
        self.z_test.data.resize_(16, self.nz).normal_(0.0, 1.0)

        for step in range(2, self.max_resl + 1 + 5):
            for iter in tqdm(range(0, (self.trns_tick * 2 + self.stab_tick * 2) * self.TICK, self.loader.batchsize)):
                self.globalIter = self.globalIter + 1
                self.stack = self.stack + self.loader.batchsize
                if self.stack > ceil(len(self.loader.dataset)):
                    self.epoch = self.epoch + 1
                    self.stack = int(self.stack % (ceil(len(self.loader.dataset))))

                # reslolution scheduler.
                self.resl_scheduler()

                # zero gradients.
                self.G.zero_grad()
                self.D.zero_grad()

                # update discriminator.
                self.x.data = self.feed_interpolated_input(self.loader.get_batch())
                if self.flag_add_noise:
                    self.x = self.add_noise(self.x)
                self.z.data.resize_(self.loader.batchsize, self.nz).normal_(0.0, 1.0)
                self.x_tilde = self.G(self.z)

                self.fx = self.D(self.x)
                self.fx_tilde = self.D(self.x_tilde.detach())

                loss_d = self.mse(self.fx.squeeze(), self.real_label) + \
                         self.mse(self.fx_tilde, self.fake_label)
                loss_d.backward()
                self.opt_d.step()

                # update generator.
                fx_tilde = self.D(self.x_tilde)
                loss_g = self.mse(fx_tilde.squeeze(), self.real_label.detach())
                loss_g.backward()
                self.opt_g.step()

                # logging.
                log_msg = ' [E:{0}][T:{1}][{2:6}/{3:6}]  errD: {4:.4f} | errG: {5:.4f} | [lr:{11:.5f}][cur:{6:.3f}][resl:{7:4}][{8}][{9:.1f}%][{10:.1f}%]'.format(
                    self.epoch, self.globalTick, self.stack, len(self.loader.dataset), loss_d.item(), loss_g.item(),
                    self.resl, int(pow(2, floor(self.resl))), self.phase, self.complete['gen'], self.complete['dis'],
                    self.lr)
                tqdm.write(log_msg)

                # save model.
                self.snapshot('repo/model')

                # save image grid.
                if self.globalIter % self.config.save_img_every == 0:
                    with torch.no_grad():
                        x_test = self.G(self.z_test)
                    utils.mkdir('repo/save/grid')
                    utils.save_image_grid(x_test.data, 'repo/save/grid/{0}_{1}_G{2:.2f}_D{3:.2f}.jpg'.format(
                        int(self.globalIter / self.config.save_img_every), self.phase, self.complete['gen'],
                        self.complete['dis']))
                    utils.mkdir('repo/save/resl_{}'.format(int(floor(self.resl))))
                    utils.save_image_single(x_test.data, 'repo/save/resl_{0}/{1}_{2}_G{3:.2f}_D{4:.2f}.jpg'.format(
                        int(floor(self.resl)), int(self.globalIter / self.config.save_img_every), self.phase,
                        self.complete['gen'], self.complete['dis']))

                # tensorboard visualization.
                if self.use_tb:
                    with torch.no_grad():
                        x_test = self.G(self.z_test)
                    self.tb.add_scalar('data/loss_g', loss_g[0].item(), self.globalIter)
                    self.tb.add_scalar('data/loss_d', loss_d[0].item(), self.globalIter)
                    self.tb.add_scalar('tick/lr', self.lr, self.globalIter)
                    self.tb.add_scalar('tick/cur_resl', int(pow(2, floor(self.resl))), self.globalIter)
                    '''IMAGE GRID
                    self.tb.add_image_grid('grid/x_test', 4, utils.adjust_dyn_range(x_test.data.float(), [-1,1], [0,1]), self.globalIter)
                    self.tb.add_image_grid('grid/x_tilde', 4, utils.adjust_dyn_range(self.x_tilde.data.float(), [-1,1], [0,1]), self.globalIter)
                    self.tb.add_image_grid('grid/x_intp', 4, utils.adjust_dyn_range(self.x.data.float(), [-1,1], [0,1]), self.globalIter)
                    '''

    def get_state(self, target):
        if target == 'gen':
            state = {
                'resl': self.resl,
                'state_dict': self.G.module.state_dict(),
                'optimizer': self.opt_g.state_dict(),
                'epoch': self.epoch,
                'globalTick': self.globalTick,
                "globalIter": self.globalIter,
                'stack': self.stack,
                'learning_rate': self.lr,
                'phase': self.phase,
                'kimgs': self.kimgs,
                'complete': self.complete['gen'],
                'flush': self.flag_flush_gen
            }
            return state
        elif target == 'dis':
            state = {
                'resl': self.resl,
                'state_dict': self.D.module.state_dict(),
                'optimizer': self.opt_d.state_dict(),
                'epoch': self.epoch,
                'globalTick': self.globalTick,
                'globalIter': self.globalIter,
                'stack': self.stack,
                'learning_rate': self.lr,
                'phase': self.phase,
                'kimgs': self.kimgs,
                'complete': self.complete['dis'],
                'flush': self.flag_flush_dis
            }
            return state
        else:
            state = {
                'resl': self.resl,
                'state_dict_D': self.D.module.state_dict(),
                'state_dict_G': self.G.module.state_dict(),
                'optimizer': self.opt_d.state_dict(),
                'epoch': self.epoch,
                'globalTick': self.globalTick,
                'globalIter': self.globalIter,
                'stack': self.stack,
                'learning_rate': self.lr,
                'phase': self.phase,
                'kimgs': self.kimgs,
                'complete_D': self.complete['dis'],
                'complete_G': self.complete['gen'],
                'flush': self.flag_flush_dis
            }
            return state


    def snapshot(self, path):
        if not os.path.exists(path):
            if os.name == 'nt':
                os.system('mkdir {}'.format(path.replace('/', '\\')))
            else:
                os.system('mkdir -p {}'.format(path))
        # save every 100 tick if the network is in stab phase.
        curr_time = datetime.now()
        ndis = 'dis_R{}_T{}_{}.pth.tar'.format(int(floor(self.resl)), self.globalTick, str(curr_time))
        ngen = 'gen_R{}_T{}_{}.pth.tar'.format(int(floor(self.resl)), self.globalTick, str(curr_time))
        if self.globalTick % 50 == 0:
            if self.phase == 'gstab' or self.phase == 'dstab' or self.phase == 'final':
                save_path = os.path.join(path, ndis)
                if not os.path.exists(save_path):
                    # Discriminator checkpoint
                    torch.save(self.get_state('dis'), save_path)
                    # Generator checkpoint
                    save_path = os.path.join(path, ngen)  # update save path
                    torch.save(self.get_state('gen'), save_path)
                    print('[snapshot] model saved @ {}'.format(path))


if __name__ == '__main__':
    ## perform training.
    print('----------------- configuration -----------------')
    for k, v in vars(config).items():
        print('  {}: {}'.format(k, v))
    print('-------------------------------------------------')
    torch.backends.cudnn.benchmark = True  # boost speed.
    trainer = trainer(config)
    trainer.train()


