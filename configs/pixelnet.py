import numpy as np
import torch
import torchvision
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from collections import namedtuple
from functools import partial
from PIL import Image

import data_transforms
import data_iterators
import pathfinder
import utils
import app

restart_from_save = None
rng = np.random.RandomState(37145)

# transformations
p_transform = {'patch_size': (256, 256),
               'channels': 3,
               'n_labels': 17}

# only lossless augmentations
p_augmentation = {
    'rot90_values': [0, 1, 2, 3],
    'flip': [0, 1]
}

# mean and std values for imagenet
mean = np.asarray([0.485, 0.456, 0.406])
mean = mean[:, None, None]
std = np.asarray([0.229, 0.224, 0.225])
std = std[:, None, None]


# data preparation function
def data_prep_fun(x, y, random_gen):
    x = np.swapaxes(x, 0, 2)
    x = np.swapaxes(x, 1, 2)
    x = x / 255.
    x = x.astype(np.float32)

    y = y / 255.
    y = y[None, :, :]
    y = y.astype(np.float32)

    x, y = data_transforms.random_crop_x_y(x, y, 256, 256, random_gen)

    return x, y


train_data_prep_fun = partial(data_prep_fun, random_gen=rng)
valid_data_prep_fun = partial(data_prep_fun, random_gen=np.random.RandomState(0))

# data iterators
batch_size = 1
nbatches_chunk = 1
chunk_size = batch_size * nbatches_chunk

# dataset1 = app.get_id_pairs('test_data/test1/trainA', 'test_data/test1_hed/trainA')
dataset1 = app.get_id_pairs('ir2day_3108/trainA', 'hed_ir2day_3108/trainA')
dataset2 = app.get_id_pairs('ir2day_3108/trainB', 'hed_ir2day_3108/trainB')
img_id_pairs = [dataset1]

id_pairs = app.train_val_test_split(img_id_pairs, train_fraction=.7, val_fraction=.15, test_fraction=.15)

bad_ids = []
id_pairs['train'] = [x for x in id_pairs['train'] if x not in bad_ids]
id_pairs['valid'] = [x for x in id_pairs['valid'] if x not in bad_ids]
id_pairs['test'] = [x for x in id_pairs['test'] if x not in bad_ids]

train_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                       batch_size=chunk_size,
                                                       img_id_pairs=id_pairs['train'],
                                                       data_prep_fun=train_data_prep_fun,
                                                       label_prep_fun=train_data_prep_fun,
                                                       rng=rng,
                                                       full_batch=True, random=True, infinite=True)

valid_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                       batch_size=chunk_size,
                                                       img_id_pairs=id_pairs['valid'],
                                                       data_prep_fun=valid_data_prep_fun,
                                                       label_prep_fun=valid_data_prep_fun,
                                                       rng=rng,
                                                       full_batch=False, random=False, infinite=False)

test_data_iterator = data_iterators.EdgeDataGenerator(mode='all',
                                                      batch_size=chunk_size,
                                                      img_id_pairs=id_pairs['test'],
                                                      data_prep_fun=valid_data_prep_fun,
                                                      label_prep_fun=valid_data_prep_fun,
                                                      rng=rng,
                                                      full_batch=False, random=False, infinite=False)

nchunks_per_epoch = train_data_iterator.nsamples // chunk_size
max_nchunks = nchunks_per_epoch * 40
print('max_nchunks', max_nchunks)

validate_every = int(0.1 * nchunks_per_epoch)
save_every = int(10 * nchunks_per_epoch)

learning_rate_schedule = {
    0: 5e-2,
    int(max_nchunks * 0.3): 2e-2,
    int(max_nchunks * 0.6): 1e-2,
    int(max_nchunks * 0.8): 3e-3,
    int(max_nchunks * 0.9): 1e-3
}


# model
class HEDnet(nn.Module):
    def __init__(self, activation=F.relu):
        self.inplanes = 64
        super(HEDnet, self).__init__()

        self.activation = activation

        self.conv1_1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=(29, 33))
        self.conv1_2 = nn.Conv2d(64, 64, kernel_size=3, stride=1, padding=1)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)

        self.conv2_1 = nn.Conv2d(64, 128, kernel_size=3, stride=1, padding=1)
        self.conv2_2 = nn.Conv2d(128, 128, kernel_size=3, stride=1, padding=1)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)

        self.conv3_1 = nn.Conv2d(128, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_2 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)
        self.conv3_3 = nn.Conv2d(256, 256, kernel_size=3, stride=1, padding=1)
        self.pool3 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)

        self.conv4_1 = nn.Conv2d(256, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv4_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.pool4 = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)

        self.conv5_1 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_2 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)
        self.conv5_3 = nn.Conv2d(512, 512, kernel_size=3, stride=1, padding=1)

        self.score_dsn1 = nn.Conv2d(64, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn2 = nn.Conv2d(128, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn3 = nn.Conv2d(256, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn4 = nn.Conv2d(512, 1, kernel_size=1, stride=1, padding=0)
        self.score_dsn5 = nn.Conv2d(512, 1, kernel_size=1, stride=1, padding=0)

        # self.upsample2 = nn.Upsample(scale_factor=2, mode='nearest')
        # self.uc2_1 = nn.Conv2d(1, 1, kernel_size=3, padding=1)
        #
        # self.upsample3 = nn.Upsample(scale_factor=4, mode='nearest')
        # self.uc3_1 = nn.Conv2d(1, 9, kernel_size=3, padding=1)
        # self.uc3_2 = nn.Conv2d(9, 1, kernel_size=3, padding=1)
        #
        # self.upsample4 = nn.Upsample(scale_factor=8, mode='nearest')
        # self.uc3_1 = nn.Conv2d(1, 9, kernel_size=3, padding=1)
        # self.uc3_2 = nn.Conv2d(9, 9, kernel_size=3, padding=1)
        # self.uc3_2 = nn.Conv2d(9, 1, kernel_size=3, padding=1)
        #
        # self.upsample5 = nn.Upsample(scale_factor=16, mode='nearest')
        # self.uc2_1 = nn.Conv2d(1, 9, kernel_size=3, padding=1)
        # self.uc2_2 = nn.Conv2d(9, 1, kernel_size=3, padding=1)

        self.deconv2 = nn.ConvTranspose2d(1, 1, kernel_size=4, stride=2)
        self.deconv3 = nn.ConvTranspose2d(1, 1, kernel_size=8, stride=4)
        self.deconv4 = nn.ConvTranspose2d(1, 1, kernel_size=16, stride=8)
        self.deconv5 = nn.ConvTranspose2d(1, 1, kernel_size=32, stride=16)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, (2. / n) ** .5)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x):
        x = self.conv1_1(x)
        x = self.activation(x)
        x = self.conv1_2(x)
        c1 = self.activation(x)

        x = self.pool1(c1)

        x = self.conv2_1(x)
        x = self.activation(x)
        x = self.conv2_2(x)
        c2 = self.activation(x)

        x = self.pool2(c2)

        x = self.conv3_1(x)
        x = self.activation(x)
        x = self.conv3_2(x)
        x = self.activation(x)
        x = self.conv3_3(x)
        c3 = self.activation(x)

        x = self.pool3(c3)

        x = self.conv4_1(x)
        x = self.activation(x)
        x = self.conv4_2(x)
        x = self.activation(x)
        x = self.conv4_3(x)
        c4 = self.activation(x)

        x = self.pool4(c4)

        x = self.conv5_1(x)
        x = self.activation(x)
        x = self.conv5_2(x)
        x = self.activation(x)
        x = self.conv5_3(x)
        c5 = self.activation(x)

        s1 = self.score_dsn1(c1)
        s2 = self.score_dsn2(c2)
        s3 = self.score_dsn3(c3)
        s4 = self.score_dsn4(c4)
        s5 = self.score_dsn5(c5)

        s2 = self.deconv2(s2)
        s3 = self.deconv3(s3)
        s4 = self.deconv4(s4)
        s5 = self.deconv5(s5)

        s1 = F.sigmoid(s1)
        s2 = F.sigmoid(s2)
        s3 = F.sigmoid(s3)
        s4 = F.sigmoid(s4)
        s5 = F.sigmoid(s5)

        print(s1.size())
        print(s2.size())
        print(s3.size())
        print(s4.size())
        print(s5.size())

        cr1 = F.pad(s1, (-28, -28, -28, -28))
        cr2 = F.pad(s2, (-33, -33, -29, -29))
        cr3 = F.pad(s3, (-34, -34, -30, -30))
        cr4 = F.pad(s4, (-36, -36, -32, -32))
        cr5 = F.pad(s5, (-40, -40, -36, -36))

        out = 0.2 * cr1 + 0.2 * cr2 + 0.2 * cr3 + 0.2 * cr4 + 0.2 * cr5

        return out


def build_model():
    net = HEDnet()
    return namedtuple('Model', ['l_out'])(net)


def _assert_no_grad(variable):
    assert not variable.requires_grad, \
        "nn criterions don't compute the gradient w.r.t. targets - please " \
        "mark these variables as volatile or not requiring gradients"


class WeightedBCELoss(nn.Module):
    def __init__(self, size_average=True):
        super(WeightedBCELoss, self).__init__()
        self.size_average = size_average

    def forward(self, input, target):
        _assert_no_grad(target)

        beta = 1 - torch.mean(target)

        # target pixel = 1 -> weight beta
        # target pixel = 0 -> weight 1-beta
        weights = 1 - beta + (2 * beta - 1) * target

        return F.binary_cross_entropy(input, target, weights, self.size_average)


class WeightedMSELoss(nn.Module):
    def __init__(self):
        super(WeightedMSELoss, self).__init__()

    def forward(self, input, target):
        _assert_no_grad(target)

        beta = 1 - torch.mean(target)

        # target pixel = 1 -> weight beta
        # target pixel = 0 -> weight 1-beta
        weights = 1 - beta + (2 * beta - 1) * target

        sq_err = (target - input) ** 2
        pos = torch.sum(target * sq_err) / torch.sum(target)
        neg = torch.sum((1 - target) * sq_err) / torch.sum(1 - target)
        print('mse pos neg', pos.data.cpu().numpy()[0], neg.data.cpu().numpy()[0], 'max',
              torch.max(input).data.cpu().numpy()[0], 'min', torch.min(input).data.cpu().numpy()[0])

        return torch.sum(weights * sq_err)


def build_objective():
    return nn.modules.BCELoss()


def build_objective2():
    return WeightedMSELoss()


def score(preds, gts):
    return app.cont_f_score(preds, gts)


def intermediate_valid_predictions(preds, pid, it_valid, n_save=10):
    path = pathfinder.METADATA_PATH + '/checkpoints/' + pid
    utils.auto_make_dir(path)
    pred_id = 0
    for batch in preds:
        for pred in batch:
            if pred_id >= n_save:
                break
            pred = 255 * pred
            pred = pred.astype(int)
            print(pred.shape())
            app.save_image(pred, path + '/' + str(it_valid) + '_' + str(pred_id) + '.jpg', mode='L')
            pred_id += 1


# # updates
# def build_updates(model, learning_rate):
#     return optim.SGD(model.parameters(), lr=learning_rate, momentum=0.9, weight_decay=0.0002)

# updates
def build_updates(model, learning_rate):
    return optim.Adam(model.parameters(), lr=learning_rate)
