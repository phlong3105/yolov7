#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import os
import socket
from threading import Thread

import click
import numpy as np
import torch
import yaml
from tqdm import tqdm

from models.experimental import attempt_load
from mon import core, DATA_DIR
from utils.datasets import create_dataloader
from utils.general import (
    box_iou, check_dataset, check_file, check_img_size, coco80_to_coco91_class, colorstr,
    increment_path, non_max_suppression, scale_coords, set_logging, xywh2xyxy, xyxy2xywh,
)
from utils.metrics import ap_per_class, ConfusionMatrix
from utils.plots import output_to_target, plot_images, plot_study_txt
from utils.torch_utils import select_device, time_synchronized, TracedModel

console       = core.console
_current_file = core.Path(__file__).absolute()
_current_dir  = _current_file.parents[0]


# region Test

def test(
    opt,
    data,
    weights        = None,
    batch_size     = 32,
    imgsz          = 640,
    conf_thres     = 0.001,
    iou_thres      = 0.5,       # for NMS
    max_det        = 300,
    save_json      = False,
    single_cls     = False,
    augment        = False,
    verbose        = False,
    model          = None,
    dataloader     = None,
    save_dir       = core.Path(""),  # for saving images
    save_txt       = False,     # for auto-labelling
    save_hybrid    = False,     # for hybrid auto-labelling
    save_conf      = False,     # save auto-label confidences
    plots          = True,
    wandb_logger   = None,
    compute_loss   = None,
    half_precision = True,
    trace          = False,
    is_coco        = False,
    v5_metric      = False,
):
    # Initialize/load model and set device
    training = model is not None
    if training:  # called by train.py
        device = next(model.parameters()).device  # get model device

    else:  # called directly
        set_logging()
        device = select_device(opt.device, batch_size=batch_size)
        
        # Directories
        # save_dir = core.Path(increment_path(core.Path(opt.project) / opt.name, exist_ok=opt.exist_ok))  # increment run
        # (core.Path(save_dir) / "labels" if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
        (save_dir / "images" if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
        (save_dir / "labels" if save_txt else save_dir).mkdir(parents=True, exist_ok=True)  # make dir
        
        # Load model
        weights = weights[0] if isinstance(weights, list | tuple) else weights
        model   = attempt_load(weights, map_location=device)  # load FP32 model
        gs      = max(int(model.stride.max()), 32)  # grid size (max stride)
        imgsz   = check_img_size(imgsz, s=gs)  # check img_size
        
        if trace:
            model = TracedModel(model, device, imgsz)

    # Half
    half = device.type != "cpu" and half_precision  # half precision only supported on CUDA
    if half:
        model.half()

    # Configure
    model.eval()
    if isinstance(data, str):
        is_coco = data.endswith("coco.yaml")
        with open(data, encoding="utf-8") as f:
            data   = yaml.load(f, Loader=yaml.SafeLoader)
            _train = data["train"]
            _val   = data["val"]
            _test  = data["test"]
            if isinstance(_train, list):
                _train = [str(DATA_DIR / t) for t in _train]
            elif _train:
                _train = str(DATA_DIR / _train)
            if isinstance(_val, list):
                _val   = [str(DATA_DIR / t) for t in _val]
            elif _val:
                _val   = str(DATA_DIR / _val)
            if isinstance(_test, list):
                _test  = [str(DATA_DIR / t) for t in _test]
            elif _test:
                _test  = str(DATA_DIR / _test)
            data["train"] = _train
            data["val"]   = _val
            data["test"]  = _test
    
    check_dataset(data)  # check
    nc   = 1 if single_cls else int(data["nc"])  # number of classes
    iouv = torch.linspace(0.5, 0.95, 10).to(device)  # iou vector for mAP@0.5:0.95
    niou = iouv.numel()

    # Logging
    log_imgs = 0
    if wandb_logger and wandb_logger.wandb:
        log_imgs = min(wandb_logger.log_imgs, 100)
    # Dataloader
    if not training:
        if device.type != "cpu":
            model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))  # run once
        task       = opt.task if opt.task in ("train", "val", "test") else "val"  # path to train/val/test images
        dataloader = create_dataloader(data[task], imgsz, batch_size, gs, opt, pad=0.5, rect=True, prefix=colorstr(f"{task}: "))[0]

    if v5_metric:
        print("Testing with YOLOv5 AP metric...")
    
    seen  = 0
    confusion_matrix = ConfusionMatrix(nc=nc)
    names = {k: v for k, v in enumerate(model.names if hasattr(model, "names") else model.module.names)}
    coco91class = coco80_to_coco91_class()
    s     = ("%20s" + "%12s" * 6) % ("Class", "Images", "Labels", "P", "R", "mAP@.5", "mAP@.5:.95")
    p, r, f1, mp, mr, map50, map, t0, t1 = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    loss  = torch.zeros(3, device=device)
    jdict, stats, ap, ap_class, wandb_images = [], [], [], [], []
    for batch_i, (img, targets, paths, shapes) in enumerate(tqdm(dataloader, desc=s)):
        img      = img.to(device, non_blocking=True)
        img      = img.half() if half else img.float()  # uint8 to fp16/32
        img     /= 255.0  # 0 - 255 to 0.0 - 1.0
        targets  = targets.to(device)
        nb, _, height, width = img.shape  # batch size, channels, height, width

        with torch.no_grad():
            # Run model
            t   = time_synchronized()
            out, train_out = model(img, augment=augment)  # inference and training outputs
            t0 += time_synchronized() - t

            # Compute loss
            if compute_loss:
                loss += compute_loss([x.float() for x in train_out], targets)[1][:3]  # box, obj, cls

            # Run NMS
            targets[:, 2:] *= torch.Tensor([width, height, width, height]).to(device)  # to pixels
            lb   = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
            t    = time_synchronized()
            out  = non_max_suppression(
                out,
                conf_thres  = conf_thres,
                iou_thres   = iou_thres,
                max_det     = max_det,
                labels      = lb,
                multi_label = True,
            )
            t1  += time_synchronized() - t

        # Statistics per image
        for si, pred in enumerate(out):
            labels  = targets[targets[:, 0] == si, 1:]
            nl      = len(labels)
            tcls    = labels[:, 0].tolist() if nl else []  # target class
            path    = core.Path(paths[si])
            seen   += 1

            if len(pred) == 0:
                if nl:
                    stats.append((torch.zeros(0, niou, dtype=torch.bool), torch.Tensor(), torch.Tensor(), tcls))
                continue

            # Predictions
            predn = pred.clone()
            scale_coords(img[si].shape[1:], predn[:, :4], shapes[si][0], shapes[si][1])  # native-space pred

            # Append to text file
            if save_txt:
                gn = torch.tensor(shapes[si][0])[[1, 0, 1, 0]]  # normalization gain whwh
                for *xyxy, conf, cls in predn.tolist():
                    xywh = (xyxy2xywh(torch.tensor(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
                    line = (cls, *xywh, conf) if save_conf else (cls, *xywh)  # label format
                    with open(save_dir / "labels" / (path.stem + ".txt"), "a") as f:
                        f.write(("%g " * len(line)).rstrip() % line + "\n")

            # W&B logging - Media Panel Plots
            if len(wandb_images) < log_imgs and wandb_logger.current_epoch > 0:  # Check for test operation
                if wandb_logger.current_epoch % wandb_logger.bbox_interval == 0:
                    box_data = [
                        {
                            "position"   : {"minX": xyxy[0], "minY": xyxy[1], "maxX": xyxy[2], "maxY": xyxy[3]},
                            "class_id"   : int(cls),
                            "box_caption": "%s %.3f" % (names[cls], conf),
                            "scores"     : {"class_score": conf},
                            "domain"     : "pixel"
                        } for *xyxy, conf, cls in pred.tolist()
                    ]
                    boxes    = {"predictions": {"box_data": box_data, "class_labels": names}}  # inference-space
                    wandb_images.append(wandb_logger.wandb.Image(img[si], boxes=boxes, caption=path.name))
            wandb_logger.log_training_progress(predn, path, names) if wandb_logger and wandb_logger.wandb_run else None

            # Append to pycocotools JSON dictionary
            if save_json:
                # [{"image_id": 42, "category_id": 18, "bbox": [258.15, 41.29, 348.26, 243.78], "score": 0.236}, ...
                image_id    = int(path.stem) if path.stem.isnumeric() else path.stem
                box         = xyxy2xywh(predn[:, :4])  # xywh
                box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
                for p, b in zip(pred.tolist(), box.tolist()):
                    jdict.append(
                        {
                            "image_id"   : image_id,
                            "category_id": coco91class[int(p[5])] if is_coco else int(p[5]),
                            "bbox"       : [round(x, 3) for x in b],
                            "score"      : round(p[4], 5)
                        }
                    )

            # Assign all predictions as incorrect
            correct = torch.zeros(pred.shape[0], niou, dtype=torch.bool, device=device)
            if nl:
                detected = []  # target indices
                tcls_tensor = labels[:, 0]

                # target boxes
                tbox = xywh2xyxy(labels[:, 1:5])
                scale_coords(img[si].shape[1:], tbox, shapes[si][0], shapes[si][1])  # native-space labels
                if plots:
                    confusion_matrix.process_batch(predn, torch.cat((labels[:, 0:1], tbox), 1))

                # Per target class
                for cls in torch.unique(tcls_tensor):
                    ti = (cls == tcls_tensor).nonzero(as_tuple=False).view(-1)  # prediction indices
                    pi = (cls == pred[:, 5]).nonzero(as_tuple=False).view(-1)  # target indices

                    # Search for detections
                    if pi.shape[0]:
                        # Prediction to target ious
                        ious, i = box_iou(predn[pi, :4], tbox[ti]).max(1)  # best ious, indices

                        # Append detections
                        detected_set = set()
                        for j in (ious > iouv[0]).nonzero(as_tuple=False):
                            d = ti[i[j]]  # detected target
                            if d.item() not in detected_set:
                                detected_set.add(d.item())
                                detected.append(d)
                                correct[pi[j]] = ious[j] > iouv  # iou_thres is 1xn
                                if len(detected) == nl:  # all targets already located in image
                                    break

            # Append statistics (correct, conf, pcls, tcls)
            stats.append((correct.cpu(), pred[:, 4].cpu(), pred[:, 5].cpu(), tcls))

        # Plot images
        if plots and batch_i < 3:
            f = save_dir / f"test_batch{batch_i}_labels.jpg"  # labels
            Thread(target=plot_images, args=(img, targets, paths, f, names), daemon=True).start()
            f = save_dir / f"test_batch{batch_i}_pred.jpg"  # predictions
            Thread(target=plot_images, args=(img, output_to_target(out), paths, f, names), daemon=True).start()

    # Compute statistics
    stats = [np.concatenate(x, 0) for x in zip(*stats)]  # to numpy
    if len(stats) and stats[0].any():
        p, r, ap, f1, ap_class = ap_per_class(*stats, plot=plots, v5_metric=v5_metric, save_dir=save_dir, names=names)
        ap50, ap               = ap[:, 0], ap.mean(1)  # AP@0.5, AP@0.5:0.95
        mp, mr, map50, map     = p.mean(), r.mean(), ap50.mean(), ap.mean()
        nt                     = np.bincount(stats[3].astype(np.int64), minlength=nc)  # number of targets per class
    else:
        nt = torch.zeros(1)

    # Print results
    pf = "%20s" + "%12i" * 2 + "%12.3g" * 4  # print format
    print(pf % ("all", seen, nt.sum(), mp, mr, map50, map))

    # Print results per class
    if (verbose or (nc < 50 and not training)) and nc > 1 and len(stats):
        for i, c in enumerate(ap_class):
            print(pf % (names[c], seen, nt[c], p[i], r[i], ap50[i], ap[i]))
    
    # Print speeds
    t = tuple(x / seen * 1E3 for x in (t0, t1, t0 + t1)) + (imgsz, imgsz, batch_size)  # tuple
    if not training:
        print("Speed: %.1f/%.1f/%.1f ms inference/NMS/total per %gx%g image at batch-size %g" % t)

    # Plots
    if plots:
        confusion_matrix.plot(save_dir=save_dir, names=list(names.values()))
        if wandb_logger and wandb_logger.wandb:
            val_batches = [wandb_logger.wandb.Image(str(f), caption=f.name) for f in sorted(save_dir.glob("test*.jpg"))]
            wandb_logger.log({"Validation": val_batches})
    if wandb_images:
        wandb_logger.log({"Bounding Box Debugger/Images": wandb_images})

    # Save JSON
    if save_json and len(jdict):
        w         = core.Path(weights[0] if isinstance(weights, list) else weights).stem if weights is not None else ''  # weights
        anno_json = "./coco/annotations/instances_val2017.json"  # annotations json
        pred_json = str(save_dir / f"{w}_predictions.json")  # predictions json
        print("\nEvaluating pycocotools mAP... saving %s..." % pred_json)
        with open(pred_json, "w") as f:
            json.dump(jdict, f)

        try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
            from pycocotools.coco     import COCO
            from pycocotools.cocoeval import COCOeval

            anno = COCO(anno_json)  # init annotations api
            pred = anno.loadRes(pred_json)  # init predictions api
            eval = COCOeval(anno, pred, "bbox")
            if is_coco:
                eval.params.imgIds = [int(core.Path(x).stem) for x in dataloader.dataset.img_files]  # image IDs to evaluate
            eval.evaluate()
            eval.accumulate()
            eval.summarize()
            map, map50 = eval.stats[:2]  # update results (mAP@0.5:0.95, mAP@0.5)
        except Exception as e:
            print(f"pycocotools unable to run: {e}")

    # Return results
    model.float()  # for training
    if not training:
        s = f"\n{len(list(save_dir.glob('labels/*.txt')))} labels saved to {save_dir / 'labels'}" if save_txt else ''
        print(f"Results saved to {save_dir}{s}")
    maps = np.zeros(nc) + map
    for i, c in enumerate(ap_class):
        maps[c] = ap[i]
    return (mp, mr, map50, map, *(loss.cpu() / len(dataloader)).tolist()), maps, t

# endregion


# region Main

@click.command(name="test", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--root",       type=str,   default=None, help="Project root.")
@click.option("--config",     type=str,   default=None, help="Model config.")
@click.option("--weights",    type=str,   default=None, help="Weights paths.")
@click.option("--model",      type=str,   default=None, help="Model name.")
@click.option("--data",       type=str,   default=None, help="Source data directory.")
@click.option("--fullname",   type=str,   default=None, help="Save results to root/run/predict/fullname.")
@click.option("--save-dir",   type=str,   default=None, help="Optional saving directory.")
@click.option("--device",     type=str,   default=None, help="Running devices.")
@click.option("--imgsz",      type=int,   default=None, help="Image sizes.")
@click.option("--conf",       type=float, default=None, help="Confidence threshold.")
@click.option("--iou",        type=float, default=None, help="IoU threshold.")
@click.option("--max-det",    type=int,   default=None, help="Max detections per image.")
@click.option("--resize",     is_flag=True)
@click.option("--benchmark",  is_flag=True)
@click.option("--save-image", is_flag=True)
@click.option("--verbose",    is_flag=True)
def main(
    root      : str,
    config    : str,
    weights   : str,
    model     : str,
    data      : str,
    fullname  : str,
    save_dir  : str,
    device    : str,
    imgsz     : int,
    conf      : float,
    iou       : float,
    max_det   : int,
    resize    : bool,
    benchmark : bool,
    save_image: bool,
    verbose   : bool,
) -> str:
    hostname = socket.gethostname().lower()
    
    # Get config args
    config   = core.parse_config_file(project_root=_current_dir / "config", config=config)
    args     = core.load_config(config)
    
    # Prioritize input args --> config file args
    root     = root     or args["root"]
    weights  = weights  or args["weights"]
    model    = model    or args["model"]
    data     = args["data"]
    project  = args["project"]
    fullname = fullname or args["name"]
    device   = device   or args["device"]
    imgsz    = imgsz    or args["imgsz"]
    conf     = conf     or args["conf"]
    iou      = iou      or args["iou"]
    max_det  = max_det  or args["max_det"]
    verbose  = verbose  or args["verbose"]
    
    # Parse arguments
    root     = core.Path(root)
    weights  = core.to_list(weights)
    model    = core.Path(model)
    model    = model if model.exists() else _current_dir / "config/training" / model.name
    model    = str(model.config_file())
    data     = core.Path(data)
    data     = data  if data.exists() else _current_dir / "data"  / data.name
    data     = str(data.config_file())
    project  = root.name or project
    save_dir = save_dir  or root / "run" / "test" / fullname
    save_dir = core.Path(save_dir)
    imgsz    = core.to_list(imgsz)
    
    # Update arguments
    args["root"]     = root
    args["config"]   = config
    args["weights"]  = weights
    args["model"]    = model
    args["data"]     = data
    args["project"]  = project
    args["name"]     = fullname
    args["save_dir"] = save_dir
    args["device"]   = device
    args["imgsz"]    = imgsz
    args["conf"]     = conf
    args["iou"]      = iou
    args["max_det"]  = max_det
    args["verbose"]  = verbose
    
    opt            = argparse.Namespace(**args)
    opt.save_json |= opt.data.endswith("coco.yaml")
    opt.data       = check_file(opt.data)  # check file
    
    # Test
    if opt.task in ["val", "test"]:  # run normally
        test(
            opt         = opt,
            data        = opt.data,
            weights     = opt.weights,
            batch_size  = opt.batch_size,
            imgsz       = opt.img_size,
            conf_thres  = opt.conf,
            iou_thres   = opt.iou,
            save_json   = opt.save_json,
            single_cls  = opt.single_cls,
            augment     = opt.augment,
            verbose     = opt.verbose,
            save_txt    = opt.save_txt | opt.save_hybrid,
            save_hybrid = opt.save_hybrid,
            save_conf   = opt.save_conf,
            trace       = not opt.no_trace,
            v5_metric   = opt.v5_metric
        )
    elif opt.task == "speed":  # speed benchmarks
        for w in opt.weights:
            test(
                opt        = opt,
                data       = opt.data,
                weights    = w,
                batch_size = opt.batch_size,
                imgsz      = opt.img_size,
                conf_thres = 0.25,
                iou_thres  = 0.45,
                save_json  = False,
                plots      = False,
                v5_metric  = opt.v5_metric,
            )
    elif opt.task == "study":  # run over a range of settings and save/plot
        # python test.py --task study --data coco.yaml --iou 0.65 --weights yolov7.pt
        x = list(range(256, 1536 + 128, 128))  # x axis (image sizes)
        for w in opt.weights:
            f = f"study_{core.Path(opt.data).stem}_{core.Path(w).stem}.txt"  # filename to save to
            y = []  # y axis
            for i in x:  # img-size
                print(f"\nRunning {f} point {i}...")
                r, _, t = test(
                    opt        = opt,
                    data       = opt.data,
                    weights    = w,
                    batch_size = opt.batch_size,
                    imgsz      = i,
                    conf_thres = opt.conf,
                    iou_thres  = opt.iou,
                    save_json  = opt.save_json,
                    plots      = False,
                    v5_metric  = opt.v5_metric,
                )
                y.append(r + t)  # results and times
            np.savetxt(f, y, fmt="%10.4g")  # save
        os.system("zip -r study.zip study_*.txt")
        plot_study_txt(x=x)  # plot
    
    return str(opt.save_dir)


if __name__ == "__main__":
    main()
    
# endregion