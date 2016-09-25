import sys
import os
import time
import string
import random
import pickle

import numpy as np
import theano
import theano.tensor as T
import lasagne
import math
import sklearn.metrics as sk
import scipy.io as sio

from lasagne.nonlinearities import rectify, softmax
from lasagne.layers import InputLayer, DenseLayer, DropoutLayer, batch_norm, BatchNormLayer
from lasagne.layers import ElemwiseSumLayer, NonlinearityLayer, GlobalPoolLayer
from lasagne.layers.dnn import Conv2DDNNLayer as ConvLayer
from lasagne.init import HeNormal
from lasagne.layers import Conv2DLayer as ConvLayer

def unpickle(file):
    import pickle
    fo = open(file, 'rb')
    dict = pickle.load(fo, encoding='latin1')
    fo.close()
    return dict

def load_data(dataset):
    xs = []
    ys = []
    if dataset == 'CIFAR-10':
        for j in range(5):
            d = unpickle('cifar-10-batches-py/data_batch_'+str(j+1))
            x = d['data']
            y = d['labels']
            xs.append(x)
            ys.append(y)

        d = unpickle('cifar-10-batches-py/test_batch')
        xs.append(d['data'])
        ys.append(d['labels'])
    if dataset == 'CIFAR-100':
        d = unpickle('cifar-100-python/train')
        x = d['data']
        y = d['fine_labels']
        xs.append(x)
        ys.append(y)

        d = unpickle('cifar-100-python/test')
        xs.append(d['data'])
        ys.append(d['fine_labels'])

    x = np.concatenate(xs)/np.float32(255)
    y = np.concatenate(ys)
    x = np.dstack((x[:, :1024], x[:, 1024:2048], x[:, 2048:]))
    x = x.reshape((x.shape[0], 32, 32, 3)).transpose(0,3,1,2)

    # subtract per-pixel mean
    pixel_mean = np.mean(x[0:50000],axis=0)
    x -= pixel_mean

    # create mirrored images
    X_train = x[0:50000,:,:,:]
    Y_train = y[0:50000]
    X_train_flip = X_train[:,:,:,::-1]
    Y_train_flip = Y_train
    X_train = np.concatenate((X_train,X_train_flip),axis=0)
    Y_train = np.concatenate((Y_train,Y_train_flip),axis=0)

    X_test = x[50000:,:,:,:]
    Y_test = y[50000:]

    return dict(
        X_train=lasagne.utils.floatX(X_train),
        Y_train=Y_train.astype('int32'),
        X_test=lasagne.utils.floatX(X_test),
        Y_test=Y_test.astype('int32'),)

# ##################### Build the neural network model #######################


def ResNet_FullPre_Wide(input_var=None, nout=10,  n=3, k=2, dropoutrate = 0):
    '''
    Adapted from https://gist.github.com/FlorianMuellerklein/3d9ba175038a3f2e7de3794fa303f1ee
    which was tweaked to be consistent with 'Identity Mappings in Deep Residual Networks', Kaiming He et al. 2016 (https://arxiv.org/abs/1603.05027)
    And 'Wide Residual Networks', Sergey Zagoruyko, Nikos Komodakis 2016 (http://arxiv.org/pdf/1605.07146v1.pdf)
    '''
    n_filters = {0:16, 1:16*k, 2:32*k, 3:64*k}

    # create a residual learning building block with two stacked 3x3 convlayers and dropout
    def residual_block(l, increase_dim=False, first=False, filters=16):
        if increase_dim:
            first_stride = (2,2)
        else:
            first_stride = (1,1)

        if first:
            # hacky solution to keep layers correct
            bn_pre_relu = l
        else:
            # contains the BN -> ReLU portion, steps 1 to 2
            bn_pre_conv = BatchNormLayer(l)
            bn_pre_relu = NonlinearityLayer(bn_pre_conv, rectify)

        # contains the weight -> BN -> ReLU portion, steps 3 to 5
        conv_1 = batch_norm(ConvLayer(bn_pre_relu, num_filters=filters, filter_size=(3,3), stride=first_stride, nonlinearity=rectify, pad='same', W=HeNormal(gain='relu')))

        if dropoutrate > 0:   # with dropout
            dropout = DropoutLayer(conv_1, p=dropoutrate)

            # contains the last weight portion, step 6
            conv_2 = ConvLayer(dropout, num_filters=filters, filter_size=(3,3), stride=(1,1), nonlinearity=None, pad='same', W=HeNormal(gain='relu'))
        else:   # without dropout
            conv_2 = ConvLayer(conv_1, num_filters=filters, filter_size=(3,3), stride=(1,1), nonlinearity=None, pad='same', W=HeNormal(gain='relu'))

        # add shortcut connections
        if increase_dim:
            # projection shortcut, as option B in paper
            projection = ConvLayer(l, num_filters=filters, filter_size=(1,1), stride=(2,2), nonlinearity=None, pad='same', b=None)
            block = ElemwiseSumLayer([conv_2, projection])
        elif first:
            # projection shortcut, as option B in paper
            projection = ConvLayer(l, num_filters=filters, filter_size=(1,1), stride=(1,1), nonlinearity=None, pad='same', b=None)
            block = ElemwiseSumLayer([conv_2, projection])
        else:
            block = ElemwiseSumLayer([conv_2, l])

        return block

    # Building the network
    l_in = InputLayer(shape=(None, 3, 32, 32), input_var=input_var)

    # first layer=
    l = batch_norm(ConvLayer(l_in, num_filters=n_filters[0], filter_size=(3,3), stride=(1,1), nonlinearity=rectify, pad='same', W=HeNormal(gain='relu')))

    # first stack of residual blocks
    l = residual_block(l, first=True, filters=n_filters[1])
    for _ in range(1,n):
        l = residual_block(l, filters=n_filters[1])

    # second stack of residual blocks
    l = residual_block(l, increase_dim=True, filters=n_filters[2])
    for _ in range(1,n):
        l = residual_block(l, filters=n_filters[2])

    # third stack of residual blocks
    l = residual_block(l, increase_dim=True, filters=n_filters[3])
    for _ in range(1,n):
        l = residual_block(l, filters=n_filters[3])

    bn_post_conv = BatchNormLayer(l)
    bn_post_relu = NonlinearityLayer(bn_post_conv, rectify)

    # average pooling
    avg_pool = GlobalPoolLayer(bn_post_relu)

    # fully connected layer
    network = DenseLayer(avg_pool, num_units=nout, W=HeNormal(), nonlinearity=softmax)

    return network

# ############################# Batch iterator ###############################

def iterate_minibatches(inputs, targets, batchsize, shuffle=False, augment=False):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        if augment:
            # as in paper :
            # pad feature arrays with 4 pixels on each side
            # and do random cropping of 32x32
            padded = np.pad(inputs[excerpt],((0,0),(0,0),(4,4),(4,4)),mode='constant')
            random_cropped = np.zeros(inputs[excerpt].shape, dtype=np.float32)
            crops = np.random.random_integers(0,high=8,size=(batchsize,2))
            for r in range(batchsize):
                random_cropped[r,:,:,:] = padded[r,:,crops[r,0]:(crops[r,0]+32),crops[r,1]:(crops[r,1]+32)]
            inp_exc = random_cropped
        else:
            inp_exc = inputs[excerpt]

        yield inp_exc, targets[excerpt]

def main(dataset = 'CIFAR-10', iscenario = 0, n=5, k = 1, num_epochs=82, model = None, irun = 0, Te = 2.0, E1 = 41, E2 = 61, E3 = 81,
         lr=0.1, lr_fac=0.1, reg_fac=0.0005, t0=math.pi/2.0, Estart = 0, dropoutrate = 0, multFactor = 1):
    # Check if CIFAR data exists
    if dataset == 'CIFAR-10':
        if not os.path.exists("./cifar-10-batches-py"):
            print("CIFAR-10 dataset can not be found. Please download the dataset from 'https://www.cs.toronto.edu/~kriz/cifar.html'.")
            return
        nout = 10
    if dataset == 'CIFAR-100':
        if not os.path.exists("./cifar-100-python"):
            print("CIFAR-100 dataset can not be found. Please download the dataset from 'https://www.cs.toronto.edu/~kriz/cifar.html'.")
            return
        nout = 100
    # Load the dataset
    print("Loading data...")
    data = load_data(dataset)
    X_train = data['X_train']
    Y_train = data['Y_train']
    X_test = data['X_test']
    Y_test = data['Y_test']

    def partion_data_in_two(dataset, dataset_labels, in_sample_labels, oos_labels):
        '''
        :param dataset: images
        :param dataset_labels: labels
        :param in_sample_labels: a list of classes which the network will/did train on
        :param oos_labels: the complement of in_sample_labels; these classes the network has never seen
        :return: the dataset partitioned into in_sample_examples, in_sample_labels,
        oos_examples, and oos_labels in that order
        '''
        _dataset = dataset[:]     # aliasing safeguard
        _dataset_labels = dataset_labels

        in_sample_idxs = np.zeros(np.shape(_dataset_labels), dtype=bool)
        ones_vec = np.ones(np.shape(_dataset_labels), dtype=int)
        for label in in_sample_labels:
            in_sample_idxs = np.logical_or(in_sample_idxs, _dataset_labels == label * ones_vec)

        return _dataset[in_sample_idxs], _dataset_labels[in_sample_idxs], \
               _dataset[np.logical_not(in_sample_idxs)], _dataset_labels[np.logical_not(in_sample_idxs)]

    # our network trains only on a subset of classes, say 6, but class number 7 might still
    # be an in-sample label: we need to squish the labels to be in {0,...,5}
    def relabel_in_sample_labels(labels):
        labels_as_list = labels.tolist()

        set_of_labels = []
        for label in labels_as_list:
            set_of_labels.append(label)
        labels_ordered = sorted(list(set(set_of_labels)))

        relabeled = np.zeros(labels.shape, dtype=np.int32)
        for i in range(len(labels_as_list)):
            relabeled[i] = labels_ordered.index(labels_as_list[i])

        return relabeled

    if False:
        nclasses_to_exclude = 3
        # random_classes = np.arange(10)
        # np.random.shuffle(random_classes)
        # to_include = list(random_classes[:10-nclasses_to_exclude])
        # to_exclude = list(random_classes[10-nclasses_to_exclude:])
        to_include = [7, 4, 9, 0, 6, 2, 1]
        to_exclude = [8, 5, 3]

        print('In-sample classes', to_include, 'OOS classes', to_exclude)

        X_train, Y_train, oos_examples, oos_labels = \
            partion_data_in_two(X_train, Y_train, to_include, to_exclude)
        X_test, Y_test, dev_oos_examples, oos_labels_dev = \
            partion_data_in_two(X_test, Y_test, to_include, to_exclude)

        # safely assumes there is an example for each in_sample class in both the training and dev class
        Y_train = relabel_in_sample_labels(Y_train)
        Y_test = relabel_in_sample_labels(Y_test)
        oos_labels = relabel_in_sample_labels(oos_labels)   # we don't actually care about these labels
        oos_labels_dev = relabel_in_sample_labels(oos_labels_dev)   # we don't actually care about these labels

        nout = 10 - nclasses_to_exclude

    elif False:
        nclasses_to_exclude = 30
        # random_classes = np.arange(100)
        # np.random.shuffle(random_classes)
        # to_include = list(random_classes[:100-nclasses_to_exclude])
        # to_exclude = list(random_classes[100-nclasses_to_exclude:])
        to_include = [7, 37, 61, 48, 46, 25, 78, 3, 96, 75, 85, 22, 72, 11, 56, 52, 77, 1, 24, 26, 8, 82, 76, 49, 62, 99, 33, 59, 10, 31, 16, 23, 38, 58, 65, 70, 95, 64, 36, 9, 73, 53, 35, 30, 83, 44, 69, 50, 45, 92, 89, 60, 17, 4, 81, 41, 88, 19, 51, 57, 86, 32, 40, 66, 28, 55, 0, 42, 27, 63]
        to_exclude = [71, 94, 98, 67, 74, 47, 39, 5, 54, 14, 15, 68, 29, 21, 12, 87, 20, 13, 97, 90, 91, 6, 93, 2, 84, 79, 80, 43, 18, 34]

        print('In-sample classes', to_include, 'OOS classes', to_exclude)

        X_train, Y_train, oos_examples, oos_labels = \
            partion_data_in_two(X_train, Y_train, to_include, to_exclude)
        X_test, Y_test, dev_oos_examples, oos_labels_dev = \
            partion_data_in_two(X_test, Y_test, to_include, to_exclude)

        # safely assumes there is an example for each in_sample class in both the training and dev class
        Y_train = relabel_in_sample_labels(Y_train)
        Y_test = relabel_in_sample_labels(Y_test)
        oos_labels = relabel_in_sample_labels(oos_labels)   # we don't actually care about these labels
        oos_labels_dev = relabel_in_sample_labels(oos_labels_dev)   # we don't actually care about these labels

        nout = 100 - nclasses_to_exclude

    # Prepare Theano variables for inputs and targets
    input_var = T.tensor4('inputs')
    target_var = T.ivector('targets')

    # Create neural network model
    print("Building model and compiling functions...")
    network = ResNet_FullPre_Wide(input_var, nout, n, k, dropoutrate)
    print("number of parameters in model: %d" % lasagne.layers.count_params(network, trainable=True))

    if model is None:
        # Create a loss expression for training, i.e., a scalar objective we want
        # to minimize (for our multi-class problem, it is the cross-entropy loss):
        prediction = lasagne.layers.get_output(network)
        loss = lasagne.objectives.categorical_crossentropy(prediction, target_var)
        loss = loss.mean()
        # add weight decay
        all_layers = lasagne.layers.get_all_layers(network)
        sh_reg_fac = theano.shared(lasagne.utils.floatX(reg_fac))
        l2_penalty = lasagne.regularization.regularize_layer_params(all_layers, lasagne.regularization.l2) * sh_reg_fac
        loss = loss + l2_penalty

        # Create update expressions for training
        # Stochastic Gradient Descent (SGD) with momentum
        params = lasagne.layers.get_all_params(network, trainable=True)
        sh_lr = theano.shared(lasagne.utils.floatX(lr))
        updates = lasagne.updates.momentum(loss, params, learning_rate=sh_lr, momentum=0.9)

        # Compile a function performing a training step on a mini-batch (by giving
        # the updates dictionary) and returning the corresponding training loss:
        train_fn = theano.function([input_var, target_var], loss, updates=updates)

    # Create a loss expression for validation/testing
    test_prediction = lasagne.layers.get_output(network, deterministic=True)
    test_loss = lasagne.objectives.categorical_crossentropy(test_prediction, target_var)

    # simplification of kl from uniform
    kl = T.log(nout) + T.sum(test_prediction * T.log(T.abs_(test_prediction) + 1e-10), axis=1, keepdims=True)

    # test_loss = test_loss.mean()
    right = T.eq(T.argmax(test_prediction, axis=1), target_var)

    # Compile a second function computing the validation loss and accuracy:
    # val_fn = theano.function([input_var, target_var], [test_loss, test_acc])
    right_wrong_fn = theano.function([input_var, target_var], [right, kl, T.max(test_prediction, axis=1)])

    with np.load(model) as f:
        param_values = [f['arr_%d' % i] for i in range(len(f.files))]
    lasagne.layers.set_all_param_values(network, param_values)

    kl_right, kl_wrong, kl_all, conf_right, conf_wrong, conf_all = [], [], [], [], [], []
    for batch in iterate_minibatches(X_test, Y_test, 500, shuffle=False):
        inputs, targets = batch
        r, kl_a, conf_a = right_wrong_fn(inputs, targets)
        r = np.array(r, dtype=bool)
        kl_all.extend(kl_a)
        kl_right.extend(kl_a[r])
        kl_wrong.extend(kl_a[np.logical_not(r)])
        conf_all.extend(conf_a)
        conf_right.extend(conf_a[r])
        conf_wrong.extend(conf_a[np.logical_not(r)])

    safe, risky = kl_right, kl_wrong
    labels = np.zeros((len(safe) + len(risky)), dtype=np.int32)
    labels[:len(safe)] += 1
    examples = np.squeeze(np.vstack((safe, risky)))

    print('Prediction confidence (mean, std):', np.mean(conf_all), np.std(conf_all))
    print('Prediction confidence right (mean, std):', np.mean(conf_right), np.std(conf_right))
    print('Prediction confidence wrong (mean, std):', np.mean(conf_wrong), np.std(conf_wrong))

    print('\nKL[p||u]: Right/Wrong classification distinction')
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    safe, risky = conf_right, conf_wrong
    labels = np.zeros((len(safe) + len(risky)), dtype=np.int32)
    labels[:len(safe)] += 1
    examples = np.squeeze(np.vstack((np.array(safe).reshape(-1, 1), np.array(risky).reshape(-1,1))))

    print('\nPrediction Confidence: Right/Wrong classification distinction')
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))



    oos_examples = sio.loadmat("./data/sun-train1.mat")['m'].T.reshape((-1,32,32,3)).transpose(0,3,1,2).astype(np.float32)
    oos_examples -= np.mean(X_train, axis=0)

    kl_oos, conf_oos = [], []
    for batch in iterate_minibatches(oos_examples, np.zeros(shape=oos_examples.shape[0], dtype=np.int32), 500, shuffle=False):
        inputs, targets = batch
        r, kl_a, conf_a = right_wrong_fn(inputs, targets)
        kl_oos.extend(kl_a)
        conf_oos.extend(conf_a)

    print('\nPrediction confidence SUN (mean, std):', np.mean(conf_oos), np.std(conf_oos))

    print('\nKL[p||u]: In/out distribution distinction (from SUN)')
    in_sample, oos = kl_all, kl_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((in_sample, oos)))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))


    print('\nPrediction Confidence: In/out distribution distinction (from SUN)')
    in_sample, oos = conf_all, conf_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((np.array(in_sample).reshape((-1,1)), np.array(oos).reshape((-1,1)))))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    print('\nKL[p||u]: In/out distribution distinction (from SUN; relative to right)')
    in_sample, oos = kl_right, kl_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((in_sample, oos)))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    print('\nPrediction Confidence: In/out distribution distinction (from SUN; relative to right)')
    in_sample, oos = conf_right, conf_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((np.array(in_sample).reshape((-1,1)), np.array(oos).reshape((-1,1)))))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    oos_examples = np.random.normal(scale=0.5, size=X_test.shape).astype(np.float32)

    kl_oos, conf_oos = [], []
    for batch in iterate_minibatches(oos_examples, np.zeros(shape=oos_examples.shape[0], dtype=np.int32), 500, shuffle=False):
        inputs, targets = batch
        r, kl_a, conf_a = right_wrong_fn(inputs, targets)
        kl_oos.extend(kl_a)
        conf_oos.extend(conf_a)

    print('\nPrediction confidence White Noise (mean, std):', np.mean(conf_oos), np.std(conf_oos))

    print('\nKL[p||u]: In/out distribution distinction (from white noise)')
    in_sample, oos = kl_all, kl_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((in_sample, oos)))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    print('\nPrediction Confidence: In/out distribution distinction (from white noise)')
    in_sample, oos = conf_all, conf_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((np.array(in_sample).reshape((-1,1)), np.array(oos).reshape((-1,1)))))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    print('\nKL[p||u]: In/out distribution distinction (from white noise; relative to right)')
    in_sample, oos = kl_right, kl_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((in_sample, oos)))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

    print('\nPrediction Confidence: In/out distribution distinction (from white noise; relative to right)')
    in_sample, oos = conf_right, conf_oos
    labels = np.zeros((len(in_sample) + len(oos)), dtype=np.int32)
    labels[:len(in_sample)] += 1
    examples = np.squeeze(np.vstack((np.array(in_sample).reshape((-1,1)), np.array(oos).reshape((-1,1)))))
    print('PR', sk.average_precision_score(labels, examples))
    print('ROC', sk.roc_auc_score(labels, examples))

if __name__ == '__main__':

    # the only input is 'iscenario' index used to reproduce the experiments given in the paper
    # scenario #1 and #2 correspond to the original multi-step learning rate decay on CIFAR-10
    # scenarios [3-6] are 4 options for our SGDR
    # scenarios [7-10] are the same options but for 2 times wider WRNs, i.e., WRN-28-20
    # scenarios [11-20] are the same as [1-10] but for CIFAR-100

    iscenario = 5   #int(sys.argv[1])
    model = './data/network_5_1_30.npz'
    dataset = 'CIFAR-10'

    #iscenario = 15   #int(sys.argv[1])
    #model = './data/network_15_1_30.npz'
    #dataset = 'CIFAR-100'

    iruns = [1,2,3,4,5]
    lr = 0.05
    lr_fac = 0.2
    reg_fac = 0.0005
    t0 = math.pi/2.0
    Te = 50
    dropoutrate = 0
    multFactor = 1
    num_epochs = 50     # TODO: make sure to modify 35 for 10, 50 for 100
    E1 = -1;    E2 = -1;     E3 = -1;   Estart = -1

    main(dataset, iscenario, 6, 2, num_epochs, model, 1, Te, E1, E2, E3, lr, lr_fac, reg_fac, t0, Estart, dropoutrate, multFactor)
