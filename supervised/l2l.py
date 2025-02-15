import random
import torch.functional as F
import torch
from torchvision.datasets.mnist import MNIST
from torchvision.transforms import PILToTensor
from torch.utils.data import DataLoader
from intrinsic.model import Intrinsic, FCIntrinsic
from sklearn.metrics import roc_curve, RocCurveDisplay
import matplotlib
from matplotlib import pyplot as plt
# matplotlib.use('Qt5Agg')

import numpy as np
import pickle


def return_from_reward(rewards, gamma):
    """
    Compute the discounted returns for each timestep from a tensor of rewards.

    Parameters:
    - rewards (torch.Tensor): Tensor containing the instantaneous rewards.
    - gamma (float): Discount factor (0 < gamma <= 1).

    Returns:
    - torch.Tensor: Tensor containing the discounted returns.
    """
    # Initialize an empty tensor to store the returns
    returns = torch.zeros_like(rewards)

    # Variable to store the accumulated return, initialized to 0
    G = 0

    # Iterate through the rewards in reverse (from future to past)
    for t in reversed(range(len(rewards))):
        # Update the return: G_t = r_t + gamma * G_{t+1}
        G = rewards[t] + gamma * G
        returns[t] = G

    return returns


def l2l_loss(logits, targets, lfxn, classes=3, power=2, window=6):
    """
    :param logits: (examples, classes)
    :param targets: (examples)
    :param classes: num classes
    :param power: higher powers encourage larger step changes
    :return:
    """
    device = logits.device
    #targets = targets.float()
    conv_1d = torch.nn.Conv1d(in_channels=1, out_channels=1, kernel_size=window, padding=1,
                              padding_mode="replicate", device=device)
    conv_1d.weight = torch.nn.Parameter(torch.ones_like(conv_1d.weight) / window)
    conv_1d.bias = torch.nn.Parameter(torch.zeros_like(conv_1d.bias))
    ce_loss = lfxn(logits, targets).view((-1,))  #
    print(ce_loss)
    filt_ce_loss = conv_1d(ce_loss.view((1, 1, -1))).flatten()
    ce_loss = filt_ce_loss[1:] - filt_ce_loss[:-1].detach()
    ce_loss = ce_loss
    print(ce_loss)
    loss = torch.sum(ce_loss) #+ torch.pow(chance_ce - ce_loss[0], 2)
    return loss


def q_loss(val_est, targets, lfxn, gamma=.9):
    action = torch.argmax(val_est, dim=1)
    with torch.no_grad():
        reward = ((action == targets).float()) - .5
    returns = return_from_reward(reward, gamma=gamma)  # lower is better
    td = (returns - val_est[:, action])
    val_loss = torch.mean(td ** 2)
    return val_loss


class Decoder:

    def __init__(self,  train_labels=(3, 7), device="cpu", lr=1e-5, size="small"):
        self.lr = lr
        self.size = size
        if size == "small":
            self.model = FCIntrinsic(num_nodes=3, node_shape=(1, 2, 81), kernel_size=4, input_mode="overwrite", device=device, through_time=True, inject_noise=False)
        elif size == "large":
            self.model = FCIntrinsic(num_nodes=5, node_shape=(1, 4, 81), kernel_size=4, input_mode="overwrite", device=device, through_time=True, inject_noise=False)
        # self.model.init_weight = torch.nn.Parameter(torch.tensor([.01], device=device))
        self.train_labels = train_labels
        self.device = device
        self.internal_feedback_loss = torch.nn.BCELoss()
        if len(self.train_labels) > 2:
            raise ValueError("implemented for binary case only")
        else:
            # is binary
            decoder = torch.empty((81, len(train_labels)), device=device) # torch.nn.Linear(in_features=9*9, out_features=len(train_labels), device=device)
            bias = torch.empty(len(train_labels), device=device)
            self.decoder = torch.nn.Parameter(torch.nn.init.xavier_normal_(decoder))
            self.bias = torch.nn.Parameter(torch.nn.init.normal_(bias) * .01)
        self.optim = torch.optim.Adam(params=[self.model.resistance,
                                              self.model.edge.init_weight,
                                              self.model.edge.plasticity,
                                              self.model.edge.chan_map,
                                              self.decoder,
                                              self.bias], lr=lr)

        self.history = []

    def forward(self, X, y):
        pool = torch.nn.MaxPool2d(3)
        img = X.float()
        img = pool(img.reshape((1, 1, img.shape[-1], -1))).squeeze()
        img = (img - img.mean()) / img.std()
        in_states = torch.zeros_like(self.model.states)
        mask = in_states.bool()
        for i in range(1):
            with torch.no_grad():
                in_states[0, 0, :] = img.detach().flatten()
                mask[0, 0, :] = True
            self.model(in_states.detach(), mask.detach())
        in_features = self.model.states[2, 0, :].flatten()
        logits = in_features @ self.decoder + self.bias # in_features.mean(dim=(1, 2)).flatten()  #
        correct = .5 * (torch.argmax(logits, dim=0) == y) - .25
        for i in range(1):
            # in_states = torch.zeros_like(self.model.states)
            # mask = in_states.bool()
            in_states[1, 0, 5] = correct
            mask[1, 0, 5] = True
            self.model(in_states, mask.detach())
        return logits

    def _fit(self, data, label_map, iter=100):
        all_logits = []
        all_labels = []
        count = 0
        for img, label in data:
            if label not in label_map:
                continue
            if count > iter:
                break
            label = label_map.index(label)
            logits = self.forward(img, label)
            all_logits.append(logits.clone())
            all_labels.append(label)
            count += 1
        return torch.stack(all_logits, dim=0), torch.tensor(all_labels, device=self.device).long()

    def l2l_fit(self, data, epochs=1000, batch_size=100, loss_mode="ce", reset_epochs=5):
        l_fxn = torch.nn.CrossEntropyLoss(reduce=False)
        data = DataLoader(data, shuffle=True, batch_size=1)
        loss = torch.tensor([0.], device=self.device)
        sched = torch.optim.lr_scheduler.StepLR(optimizer=self.optim, gamma=.25, step_size=1000)
        for epoch in range(epochs):
            self.optim.zero_grad()
            std_model = self.instantiate()
            flipped_model = self.instantiate()
            flipped_model.train_labels = list(reversed(self.train_labels))
            if (epoch % reset_epochs) == 0:
                std_model.model.detach(reset_intrinsic=True)
                flipped_model.model.detach(reset_intrinsic=True)
            else:
                std_model.model.detach(reset_intrinsic=False)
                flipped_model.model.detach(reset_intrinsic=False)
            logits, labels = std_model._fit(data, self.train_labels, batch_size)
            f_logits, f_labels = flipped_model._fit(data, flipped_model.train_labels, batch_size)
            # loss = torch.sum(logits)
            if loss_mode == "ce":
                l_loss = torch.mean(l_fxn(logits, labels))
                fl_loss = torch.mean(l_fxn(f_logits, f_labels))
            elif loss_mode == "l2l":
                l_loss =  l2l_loss(logits, labels, l_fxn)
                fl_loss = l2l_loss(f_logits, f_labels, l_fxn)
            elif loss_mode == "both":
                l_loss = .5 * l2l_loss(logits, labels, l_fxn) + .5 * torch.mean(l_fxn(logits, labels)) #
                fl_loss = .5 * l2l_loss(f_logits, f_labels, l_fxn) + .5 * torch.mean(l_fxn(f_logits, f_labels))
            else:
                raise ValueError
            reg = torch.sum(torch.pow(self.model.edge.chan_map, 2)) + torch.sum(torch.abs(self.model.edge.plasticity))
            reg.retain_grad()
            self.history.append((l_loss.detach().cpu().item() + l_loss.detach().cpu().item()) / 2)
            print("Epoch", epoch, "loss is", self.history[-1])
            loss = l_loss + fl_loss + .001 * reg
            print('REG', .001 * reg)
            # init_plast = self.model.edge.chan_map.clone()
            loss.backward()
            self.optim.step()
            sched.step()
            loss = torch.zeros_like(loss)
            # print("change:", init_plast - self.model.edge.chan_map.clone())

    def forward_fit(self, data, iter, use_labels=None):
        self.model.detach(reset_intrinsic=True)
        if use_labels is None:
            use_labels = self.train_labels
        l_fxn = torch.nn.CrossEntropyLoss()
        data = DataLoader(data, shuffle=True, batch_size=1)
        with torch.no_grad():
            logits, labels = self._fit(data, use_labels, iter)
            # loss = l2l_loss(logits, labels, l_fxn)
        # print("Self Learn Loss:", loss.detach().item())

    def evaluate(self, data, iter, use_labels=None):
        if use_labels is None:
            use_labels = self.train_labels
        l_fxn = torch.nn.CrossEntropyLoss()
        data = DataLoader(data, shuffle=True, batch_size=1)
        with torch.no_grad():
            logits, labels = self._fit(data, use_labels, iter)
        labels = labels.long().flatten()
        probs = torch.softmax(logits, dim=1)[:, 1].flatten()
        avg_loss = l_fxn(logits, labels)
        preds = torch.argmax(logits, dim=1).flatten()
        acc = torch.count_nonzero(preds.int() == labels.int()) / len(labels)
        print(iter, "Iterations, avg CE:", avg_loss.detach().item(), "acc:", acc.detach().item())
        probs = probs.detach().cpu().float().numpy()
        labels = labels.detach().cpu().float().numpy()
        return acc, probs, labels

    def to(self, device):
        self.device = device
        self.decoder = torch.nn.Parameter(self.decoder.to(device))
        self.bias = torch.nn.Parameter(self.bias.to(device))
        self.model = self.model.to(device)
        return self

    def instantiate(self):
        new_model = Decoder(train_labels=self.train_labels, device=self.device, lr=self.lr, size=self.size)
        new_model.model = self.model.instantiate()
        new_model.decoder = self.decoder.clone()
        new_model.bias = self.bias.clone()
        return new_model
