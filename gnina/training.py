"""
PyTorch implementation of GNINA scoring function's Caffe training script.
"""

import argparse
import os
import sys
from collections import defaultdict
from typing import List, Optional

import molgrid
import numpy as np
import pandas as pd
import torch
from ignite.contrib.handlers import ProgressBar
from ignite.engine import Engine, Events
from ignite.handlers import Checkpoint, timing
from torch import nn, optim

from gnina import metrics, setup, utils
from gnina.dataloaders import GriddedExamplesLoader
from gnina.losses import AffinityLoss
from gnina.models import models_dict, weights_and_biases_init


def options(args: Optional[List[str]] = None):
    """
    Define options and parse arguments.

    Parameters
    ----------
    args: Optional[List[str]]
        List of command line arguments
    """
    parser = argparse.ArgumentParser(
        description="GNINA scoring function",
    )

    # Data
    # TODO: Allow multiple train files?
    parser.add_argument("trainfile", type=str, help="Training file")
    parser.add_argument("--testfile", type=str, default=None, help="Test file")
    parser.add_argument(
        "-d",
        "--data_root",
        type=str,
        default="",
        help="Root folder for relative paths in train files",
    )
    parser.add_argument(
        "--balanced", action="store_true", help="Balanced sampling of receptors"
    )
    parser.add_argument(
        "--no_shuffle",
        action="store_false",
        help="Deactivate random shuffling of samples",
        dest="shuffle",  # Variable name (shuffle is False when --no_shuffle is used)
    )
    parser.add_argument(
        "--label_pos", type=int, default=0, help="Pose label position in training file"
    )
    parser.add_argument(
        "--affinity_pos",
        type=int,
        default=None,
        help="Affinity value position in training file",
    )
    parser.add_argument(
        "--stratify_receptor",
        action="store_true",
        help="Sample uniformly across receptors",
    )
    parser.add_argument(
        "--ligmolcache",
        type=str,
        default="",
        help=".molcache2 file for ligands",
    )
    parser.add_argument(
        "--recmolcache",
        type=str,
        default="",
        help=".molcache2 file for receptors",
    )
    parser.add_argument(
        "-o", "--out_dir", type=str, default=os.getcwd(), help="Output directory"
    )

    # Scoring function
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default="default2017",
        help="Model name",
        choices=[k[0] for k in models_dict.keys()],  # Model names
    )
    parser.add_argument("--dimension", type=float, default=23.5, help="Grid dimension")
    parser.add_argument("--resolution", type=float, default=0.5, help="Grid resolution")
    # TODO: ligand type file and receptor type file (default: 28 types)

    # Learning
    parser.add_argument(
        "--base_lr", type=float, default=0.01, help="Base (initial) learning rate"
    )
    parser.add_argument("--momentum", type=float, default=0.9, help="Momentum")
    parser.add_argument(
        "--weight_decay", type=float, help="Weight decay", default=0.001
    )
    parser.add_argument("--batch_size", type=int, default=64, help="Batch size")
    parser.add_argument(
        "--no_random_rotation",
        action="store_false",
        help="Deactivate random rotation of samples",
        dest="random_rotation",
    )
    parser.add_argument(
        "--random_translation", type=float, default=6.0, help="Random translation"
    )
    parser.add_argument(
        "-i",
        "--iterations",
        type=int,
        default=250000,
        help="Number of iterations (epochs)",
    )
    parser.add_argument(
        "--iteration_scheme",
        type=str,
        default="small",
        help="molgrid iteration scheme",
        choices=setup._iteration_schemes.keys(),
    )
    # lr_dynamic, originally called --dynamic
    parser.add_argument(
        "--lr_dynamic",
        action="store_true",
        help="Adjust learning rate in response to training",
    )
    # lr_patience, originally called --step_when
    # Acts on epochs, not on iterations
    parser.add_argument(
        "--lr_patience",
        type=int,
        default=5,
        help="Number of epochs without improvement before learning rate update",
    )
    # lr_reduce, originally called --step_reduce
    parser.add_argument(
        "--lr_reduce", type=float, default=0.1, help="Learning rate reduction factor"
    )
    # lr_min  default value set to match --step_end_cnt default value (3 reductions)
    parser.add_argument("--lr_min", type=float, default=0.01 * 0.1 ** 3)
    parser.add_argument(
        "--clip_gradients",
        type=float,
        default=10.0,
        help="Gradients threshold (for clipping)",
    )
    parser.add_argument(
        "--pseudo_huber_affinity_loss",
        action="store_true",
        help="Use pseudo-Huber loss for affinity loss",
    )
    parser.add_argument(
        "--delta_affinity_loss",
        type=float,
        default=4.0,
        help="Delta factor for affinity loss",
    )
    parser.add_argument(
        "--scale_affinity_loss",
        type=float,
        default=1.0,
        help="Scale factor for affinity loss",
    )
    parser.add_argument(
        "--penalty_affinity_loss",
        type=float,
        default=1.0,
        help="Penalty for affinity loss",
    )

    # Misc
    parser.add_argument(
        "-t", "--test_every", type=int, default=1000, help="Test interval"
    )
    parser.add_argument(
        "--checkpoint_every",
        type=int,
        default=100,
        help="Number of epochs per checkpoint",
    )
    parser.add_argument(
        "--num_checkpoints", type=int, default=1, help="Number of checkpoints to keep"
    )
    parser.add_argument("--progress_bar", action="store_true", help="Show progress bar")
    parser.add_argument("-g", "--gpu", type=str, default="cuda:0", help="Device name")
    # ROC AUC fails when there is only one class (i.e. all poses are good poses)
    # This happens when training with crystal structures only
    parser.add_argument(
        "--no_roc_auc",
        action="store_false",
        help="Disable ROC AUC (useful for crystal poses)",
        dest="roc_auc",
    )

    parser.add_argument("-s", "--seed", type=int, default=None, help="Random seed")
    parser.add_argument("--silent", action="store_true", help="No console output")

    return parser.parse_args(args)


def _train_step_pose(
    trainer: Engine,
    batch,
    model: nn.Module,
    optimizer,
    pose_loss: nn.Module,
    clip_gradients: float,
) -> float:
    """
    Training step for pose prediction.

    Parameters
    ----------
    trainer: Engine
        PyTorch Ignite engine for training
    batch:
        Batch of data
    model:
        PyTorch model
    optimizer:
        PyTorch optimizer
    pose_loss:
        Loss function for pose prediction
    clip_gradients:
        Gradient clipping threshold

    Returns
    -------
    float
        Loss

    Notes
    -----
    Gradients are clipped by norm and not by value.
    """
    model.train()
    optimizer.zero_grad()

    # Data is already on the correct device thanks to the ExampleProvider
    grids, labels = batch

    pose_log = model(grids)

    # Compute loss for pose prediction
    loss = pose_loss(pose_log, labels)

    loss.backward()

    # TODO: Double check that gradient clipping by norm corresponds to the Caffe
    # implementation
    nn.utils.clip_grad_norm_(model.parameters(), clip_gradients)
    optimizer.step()

    return loss.item()


def _train_step_pose_and_affinity(
    trainer: Engine,
    batch,
    model: nn.Module,
    optimizer,
    pose_loss: nn.Module,
    affinity_loss: nn.Module,
    clip_gradients: float,
) -> float:
    """
    Training step for pose and affinity prediction.

    Parameters
    ----------
    trainer: Engine
        PyTorch Ignite engine for training
    batch:
        Batch of data
    model:
        PyTorch model
    optimizer:
        PyTorch optimizer
    pose_loss:
        Loss function for pose prediction
    affinity_loss:
        Loss function for binding affinity prediction
    clip_gradients:
        Gradient clipping threshold

    Returns
    -------
    float
        Loss

    Notes
    -----
    Gradients are clipped by norm and not by value.
    """
    model.train()
    optimizer.zero_grad()

    # Data is already on the correct device thanks to the ExampleProvider
    grids, labels, affinities = batch

    pose_log, affinities_pred = model(grids)

    # Compute combined loss for pose prediction and affinity prediction
    loss = pose_loss(pose_log, labels) + affinity_loss(affinities_pred, affinities)

    loss.backward()

    # TODO: Double check that gradient clipping by norm corresponds to the Caffe
    # implementation
    nn.utils.clip_grad_norm_(model.parameters(), clip_gradients)
    optimizer.step()

    return loss.item()


def _setup_trainer(
    model, optimizer, pose_loss, affinity_loss, clip_gradients: float
) -> Engine:
    """
    Setup training engine for binding pose prediction or binding pose and affinity
    prediction.

    Patameters
    ----------
    model:
        Model to train
    optimizer:
        Optimizer
    pose_loss:
        Loss function for pose prediction
    affinity_loss:
        Loss function for affinity prediction
    clip_gradients:
        Gradient clipping threshold

    Notes
    -----
    If :code:`affinity_loss is Non e`, the model return both pose and affinity
    predictions, which requites a custom training step to evaluate the combine loss
    function. The custom training step is defined in
    :fun:`_train_step_pose_and_affinity`.
    """
    if affinity_loss is not None:
        # Pose prediction and binding affinity prediction
        # Create engine based on custom train step
        trainer = Engine(
            lambda trainer, batch: _train_step_pose_and_affinity(
                trainer,
                batch,
                model,
                optimizer,
                pose_loss=pose_loss,
                affinity_loss=affinity_loss,
                clip_gradients=clip_gradients,
            )
        )
    else:
        # Pose prediction and binding affinity prediction
        # Create engine based on custom train step
        trainer = Engine(
            lambda trainer, batch: _train_step_pose(
                trainer,
                batch,
                model,
                optimizer,
                pose_loss=pose_loss,
                clip_gradients=clip_gradients,
            )
        )

    return trainer


def _evaluation_step_pose_and_affinity(evaluator: Engine, batch, model):
    """
    Evaluate model for binding pose and affinity prediction.

    Parameters
    ----------
    evaluator:
        PyTorch Ignite :code:`Engine`
    batch:
        Batch data
    model:
        Model

    Returns
    -------
    Tuple[torch.Tensor]
        Class probabilities for pose prediction, affinity prediction, true pose labels
        and experimental binding affinities

    Notes
    -----
    The model returns the log softmax of the last linear layer for binding pose
    prediction (log class probabilities) and the raw output of the last linear layer for
    binding affinity predictions.
    """
    model.eval()
    with torch.no_grad():
        grids, labels, affinities = batch
        pose_log, affinities_pred = model(grids)

    output = {
        "pose_log": pose_log,
        "affinities_pred": affinities_pred,
        "labels": labels,
        "affinities": affinities,
    }

    return output


def _evaluation_step_pose(evaluator: Engine, batch, model):
    """
    Evaluate model for binding pose prediction only.

    Parameters
    ----------
    evaluator:
        PyTorch Ignite :code:`Engine`
    batch:
        Batch data
    model:
        Model

    Returns
    -------
    Tuple[torch.Tensor]
        Class probabilities for pose prediction and true pose labels

    Notes
    -----
    While not strictly necessary (the default PyTorch Ignite evaluator would work well
    in the case of pose-prediction only), this function is used to return a dictionary
    of the output with the same key used in :fun:`_evaluation_step_pose_and_affinity`.
    This allows to simplify the code of the learning rate scheduler function. This
    function also allows consistency in allowing the use of
    :fun:`transforms.output_transform_select_pose` for both pose prediction only and
    binding pose prediction with binding affinity prediction.
    """
    model.eval()
    with torch.no_grad():
        grids, labels = batch
        pose_log = model(grids)

    output = {
        "pose_log": pose_log,
        "labels": labels,
    }

    return output


def _setup_evaluator(model, metrics, affinity: bool = False) -> Engine:
    """
    Setup PyTorch Ignite :code:`Engine` for evaluation.

    Parameters
    ----------
    model:
        PyTorch model
    metrics:
        Evaluation metrics
    affinity: bool
        Flag for affinity prediction (in addition to pose prediction)

    Returns
    -------
    ignite.Engine
        PyTorch Ignite engine for evaluation

    Notes
    -----
    For pose prediction the model is rather standard (single outpout) and therefore
    the :code:`create_supervised_evaluator()` factory function is used. For both pose
    and binding affinity prediction, the custom
    :code:`_evaluation_step_pose_and_affinity` is used instead.
    """
    if affinity:
        evaluator = Engine(
            lambda evaluator, batch: _evaluation_step_pose_and_affinity(
                evaluator, batch, model
            )
        )
    else:
        evaluator = Engine(
            lambda evaluator, batch: _evaluation_step_pose(evaluator, batch, model)
        )

    # Add metrics to the evaluator engine
    # Metrics need an output_tranform method in order to select the correct output
    # from _evaluation_step_pose_and_affinity
    for name, metric in metrics.items():
        metric.attach(evaluator, name)

    return evaluator


def training(args):
    """
    Main function for training GNINA scoring function.

    Parameters
    ----------
    args:
        Command line arguments

    Notes
    -----
    Training might start off slow because the :code:`molgrid.ExampleProvider` is caching
    the structures that are read from .gninatypes files. The training then speeds up
    considerably.
    """

    # Create necessary directories if not already present
    os.makedirs(args.out_dir, exist_ok=True)

    # Define output streams for logging
    logfile = open(os.path.join(args.out_dir, "training.log"), "w")
    if not args.silent:
        outstreams = [sys.stdout, logfile]
    else:
        outstreams = [logfile]

    # Print command line arguments
    for outstream in outstreams:
        utils.print_args(args, "--- GNINA TRAINING ---", stream=outstream)

    # Set random seed for reproducibility
    if args.seed is not None:
        molgrid.set_random_seed(args.seed)
        torch.manual_seed(args.seed)
        np.random.seed(args.seed)

    # Set device
    device = utils.set_device(args.gpu)

    # Create example providers
    train_example_provider = setup.setup_example_provider(
        args.trainfile, args, training=True
    )
    if args.testfile is not None:
        test_example_provider = setup.setup_example_provider(
            args.testfile, args, training=False
        )

    # Create grid maker
    grid_maker = setup.setup_grid_maker(args)

    train_loader = GriddedExamplesLoader(
        example_provider=train_example_provider,
        grid_maker=grid_maker,
        label_pos=args.label_pos,
        affinity_pos=args.affinity_pos,
        random_translation=args.random_translation,
        random_rotation=args.random_rotation,
        device=device,
    )

    if args.testfile is not None:
        test_loader = GriddedExamplesLoader(
            example_provider=test_example_provider,
            grid_maker=grid_maker,
            label_pos=args.label_pos,
            affinity_pos=args.affinity_pos,
            random_translation=args.random_translation,
            random_rotation=args.random_rotation,
            device=device,
        )

        assert test_loader.dims == train_loader.dims

    affinity: bool = True if args.affinity_pos is not None else False

    # Create model
    # Select model based on architecture and affinity flag (pose vs affinity)
    model = models_dict[(args.model, affinity)](train_loader.dims).to(device)
    model.apply(weights_and_biases_init)

    # Compile model into TorchScript
    model = torch.jit.script(model)

    optimizer = optim.SGD(
        model.parameters(),
        lr=args.base_lr,
        momentum=args.momentum,
        weight_decay=args.weight_decay,
    )

    # Define loss functions
    pose_loss = torch.jit.script(nn.NLLLoss())
    affinity_loss = (
        torch.jit.script(
            AffinityLoss(
                delta=args.delta_affinity_loss,
                penalty=args.penalty_affinity_loss,
                pseudo_huber=args.pseudo_huber_affinity_loss,
                scale=args.scale_affinity_loss,
            )
        )
        if affinity
        else None
    )

    trainer = _setup_trainer(
        model,
        optimizer,
        pose_loss=pose_loss,
        affinity_loss=affinity_loss,
        clip_gradients=args.clip_gradients,
    )

    allmetrics = metrics.setup_metrics(
        affinity, pose_loss, affinity_loss, args.roc_auc, device
    )

    # Storage for metrics
    metrics_train = defaultdict(list)
    metrics_test = defaultdict(list)

    evaluator = _setup_evaluator(model, allmetrics, affinity=affinity)

    # Define LR scheduler
    if args.lr_dynamic:
        torch_scheduler = optim.lr_scheduler.ReduceLROnPlateau(
            optimizer,
            mode="max",
            factor=args.lr_reduce,
            patience=args.lr_patience,
            min_lr=args.lr_min,
            verbose=False,
        )

    # Elapsed time timer, training time only
    elapsed_time = timing.Timer()
    elapsed_time.attach(
        trainer,
        start=Events.STARTED,
        resume=Events.EPOCH_STARTED,
        pause=Events.EPOCH_COMPLETED,
        step=Events.EPOCH_COMPLETED,
    )

    @trainer.on(Events.EPOCH_COMPLETED(every=args.test_every))
    def log_training_results(trainer):
        """
        Evaluate metrics on the training set and update the LR according to the loss
        function, if needed.
        """
        evaluator.run(train_loader)

        for outstream in outstreams:
            utils.log_print(
                evaluator.state.metrics,
                title="Train Results",
                epoch=trainer.state.epoch,
                epoch_time=trainer.state.times["EPOCH_COMPLETED"],
                elapsed_time=elapsed_time.total,
                stream=outstream,
            )

        mts = evaluator.state.metrics
        metrics_train["Epoch"].append(trainer.state.epoch)
        for key, value in mts.items():
            metrics_train[key].append(value)

        # Update LR based on the loss on the training set
        if args.lr_dynamic:
            loss = mts["Loss (pose)"]
            try:
                loss += mts["Loss (affinity)"]
            except KeyError:
                # No affinity loss
                pass

            torch_scheduler.step(loss)

            assert len(optimizer.param_groups) == 1
            for oustream in outstreams:
                print(
                    f"    Learning rate: {optimizer.param_groups[0]['lr']}",
                    file=oustream,
                )

    if args.testfile is not None:

        @trainer.on(Events.EPOCH_COMPLETED(every=args.test_every))
        def log_test_results(trainer):
            evaluator.run(test_loader)

            for outstream in outstreams:
                utils.log_print(
                    evaluator.state.metrics,
                    title="Test Results",
                    epoch=trainer.state.epoch,
                    stream=outstream,
                )

            metrics_test["Epoch"].append(trainer.state.epoch)
            for key, value in evaluator.state.metrics.items():
                metrics_test[key].append(value)

    # TODO: Save input parameters as well
    # TODO: Save best models (lower loss)
    to_save = {"model": model, "optimizer": optimizer}
    # Requires no checkpoint in the output directory
    # Since checkpoints are not automatically removed when restarting, it would be
    # dangerous to run without requiring the directory to have no previous checkpoints
    checkpoint = Checkpoint(
        to_save,
        args.out_dir,
        n_saved=args.num_checkpoints,
        global_step_transform=lambda *_: trainer.state.epoch,
    )
    trainer.add_event_handler(
        Events.EPOCH_COMPLETED(every=args.checkpoint_every), checkpoint
    )

    if args.progress_bar:
        pbar = ProgressBar()
        pbar.attach(trainer)

    trainer.run(train_loader, max_epochs=args.iterations)

    pd.DataFrame(metrics_train).to_csv(
        os.path.join(args.out_dir, "metrics_train.csv"),
        float_format="%.5f",
        index=False,
    )

    if args.testfile is not None:
        pd.DataFrame(metrics_test).to_csv(
            os.path.join(args.out_dir, "metrics_test.csv"),
            float_format="%.5f",
            index=False,
        )

    # Close log file
    logfile.close()


if __name__ == "__main__":
    args = options()
    training(args)
