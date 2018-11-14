import numpy as np
import torch.nn.functional as F
from sklearn.preprocessing import OneHotEncoder
import torch
import cv2
import os
import matplotlib.pyplot as plt
from tqdm import tqdm


def eval_batch(sample, features, fc):
    # We don't need gradient to make an inference  for the features
    with torch.no_grad():
        inputs = sample['img'].to("cuda")
        bs, ncrops, c, h, w = inputs.size()
        maps = features(inputs.view(-1, c, h, w))

    fc.zero_grad()
    # Registering a hook to get the gradients
    grads = []
    maps_avg = F.adaptive_avg_pool2d(maps, 1).view(maps.size(0), -1)
    # First we should attach the variable back to the graph
    maps_avg.requires_grad = True
    # Now registering the backward hook
    maps_avg.register_hook(lambda x: grads.append(x))

    # Making the inference
    # Applying the TTA right away during the forward pass
    out_tmp = F.softmax(fc(maps_avg), 1).view(bs, ncrops, -1).mean(1)
    probs_not_summed = out_tmp.to("cpu").detach().numpy()
    # Summing the probabilities values for progression
    # This allows us to predict progressor / non-progressor
    out = torch.cat((out_tmp[:, 0].view(-1, 1), out_tmp[:, 1:].sum(1).view(-1, 1)), 1)
    # Saving the results to CPU
    probs = out.to("cpu").detach().numpy()

    # Using simple one hot encoder to create a fake gradient
    ohe = OneHotEncoder(sparse=False, n_values=out.size(1))
    # Creating the fake gradient (read the paper for details)
    index = np.argmax(probs, axis=1).reshape(-1, 1)
    fake_grad = torch.from_numpy(ohe.fit_transform(index)).float().to('cuda')
    # Backward pass after which we'll have the gradients
    out.backward(fake_grad)

    # Reshaping the activation maps sand getting the weights using the stored gradients
    # This way we would be able to consider GradCAM for each crop individually

    # Making the GradCAM
    # Going over the batch
    weight = grads[-1]
    with torch.no_grad():
        weighted_A = weight.unsqueeze(-1).unsqueeze(-1).expand(*maps.size()).mul(maps)
        gcam_batch = F.relu(weighted_A).view(bs, ncrops, -1, maps.size(-2), maps.size(-1)).sum(2)
        gcam_batch = gcam_batch.to('cpu').numpy()

    return gcam_batch, probs_not_summed


def preds_and_hmaps(rs_result, gradcams, dataset_root, figsize, threshold, savepath):
    ids_rs = []
    hmaps = []

    w, h = 310, 310
    size = (300, 300)
    x1 = w // 2 - size[0] // 2
    y1 = h // 2 - size[1] // 2

    for i, entry in tqdm(rs_result.iterrows(), total=rs_result.shape[0]):
        if entry.pred < threshold or entry.Progressor == 0:
            continue
        img = cv2.imread(os.path.join(dataset_root, f'{entry.ID}_00_{entry.Side}.png'), 0)

        if 'L' == entry.Side:
            img = cv2.flip(img, 1)

        img = cv2.resize(img, (w, h))

        tmp = np.zeros((h, w))
        # Center crop
        tmp[y1:y1 + size[0], x1:x1 + size[1]] += cv2.resize(gradcams[i, 0, :, :], size)
        # Upper-left crop
        tmp[0:size[0], 0:size[1]] += cv2.resize(gradcams[i, 1, :, :], size)
        # Upper-right crop
        tmp[0:size[0], w - size[1]:w] += cv2.resize(gradcams[i, 2, :, :], size)
        # Bottom-left crop
        tmp[h - size[0]:h, 0:size[1]] += cv2.resize(gradcams[i, 3, :, :], size)
        # Bottom-right crop
        tmp[h - size[0]:h, w - size[1]:w] += cv2.resize(gradcams[i, 4, :, :], size)

        tmp = tmp[y1:y1 + size[0], x1:x1 + size[1]]
        tmp -= tmp.min()
        tmp /= tmp.max()
        tmp *= 255

        hmaps.append(tmp)
        ids_rs.append(entry.ID)
        img = img[y1:y1 + size[0], x1:x1 + size[1]]

        plt.figure(figsize=(figsize, figsize))
        plt.subplot(121)
        plt.title(f'Original Image {entry.ID} | Prog. {entry.KL} -> {entry.KL+entry.Prog_increase}')
        plt.imshow(img, cmap=plt.cm.Greys_r)
        plt.xticks([])
        plt.yticks([])

        plt.subplot(122)
        plt.title(f'GradCAM {entry.ID}')
        plt.imshow(img, cmap=plt.cm.Greys_r)
        plt.imshow(tmp, cmap=plt.cm.jet, alpha=0.5)
        plt.xticks([])
        plt.yticks([])
        plt.savefig(os.path.join(savepath, f'{entry.ID}_{entry.Side}.pdf'), bbox_inches='tight')
        plt.close()