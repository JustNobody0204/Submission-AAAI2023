# Copyright 2022 The Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
    Main program of training. 
"""

import functools
import os
import time
from absl import flags
from absl import logging
import flax
from flax import jax_utils
from flax.metrics import tensorboard
from gnp.training import utli
import jax
import jax.numpy as jnp
from tensorflow.io import gfile
from gnp.training import lr_schedule
from gnp.training import training_core
from gnp.ds_pipeline.get_dataset import dataset_source

FLAGS = flags.FLAGS


def train(model : flax.linen.Module,
          optimizer : flax.optim.Optimizer,
          state : flax.core.frozen_dict.FrozenDict,
          dataset_source : dataset_source.DatasetSource,
          working_dir : str,
          num_epochs : int) -> None:

    """
        Main program of training the given model. 

        Args:
            model : the flax model.
            optimizer : the optimizer to use to train the model.
            state : the state of the associated parameters in the model,
              generally referring to the moving average in BN layer.
            dataset_source : the dataset pipeline.
            working_dir : the path to the working folder, where tensorboard
              and the halfway checkpoints will be stored.
            num_epochs : the total number of epochs used for training.
    """

    # Preparations of checkpoints and tensorboards.
    checkpoint_dir = os.path.join(working_dir, 'checkpoints')
    summary_writer = tensorboard.SummaryWriter(working_dir)
    if jax.host_id() != 0:
        summary_writer.scalar = lambda *args: None

    # Check and launch previous saved latest checkpoint with the 
    # same config deployments.
    if gfile.exists(checkpoint_dir):
        optimizer, state, epoch_last_checkpoint = utli.restore_checkpoint(
            optimizer, state, checkpoint_dir)
        initial_epoch = epoch_last_checkpoint + 1
        info = 'Resuming training from epoch {}'.format(initial_epoch)
        logging.info(info)

        # Terminate training directly if the epoch in the resumed
        # ckpt exceeds the target training total epoch.
        if initial_epoch >= num_epochs:
            logging.info("The epochs in checkpoints exceeds the total epochs for training!")
            logging.info("Terminate training!")
            return
    else:
        initial_epoch = jnp.array(0, dtype=jnp.int32)
        logging.info("Starting training from scratch.")

    # Replicate the optimizer and state to each available devices.
    optimizer = jax_utils.replicate(optimizer)
    state = jax_utils.replicate(state)

    # PRNG Key for rngs that exist in the model.
    prng_key = jax.random.PRNGKey(FLAGS.config.seeds)

    # Get learning rate schedule
    learning_rate_fn = lr_schedule.get_learning_schedule(
        lr_schedule_type = FLAGS.config.lr_schedule_type,
        base_lr = FLAGS.config.base_lr,
        num_epochs = num_epochs,
        num_trainig_samples = dataset_source.num_training_obs,
        batch_size = dataset_source.batch_size,
        warmup_epochs = FLAGS.config.warmup_epochs,
    )
    if FLAGS.config.use_hybrid_training:
        if FLAGS.config.schedule_function_type != "none":
            logging.info(f"Use scheduling function type : {FLAGS.config.schedule_function_type}")
            generate_scheduling_probability = utli.create_scheduling_function(
                FLAGS.config.schedule_function_type,
                num_epochs = num_epochs,
                num_training_samples = dataset_source.num_training_obs,
            )
        else:
            generate_scheduling_probability = FLAGS.config.schedule_function(**FLAGS.config.hybrid_config)
    else:
        generate_scheduling_probability = None
    
    # PMAP the training and evaluate functions. This will pass the
    # functions to each device to execute later.
    pmapped_train_step = jax.pmap(
        functools.partial(
            training_core.train_step,
            learning_rate_fn=learning_rate_fn,
            l2_reg=FLAGS.config.l2_regularization,
            r = FLAGS.config.gnp.r,
            apply_fn = model.apply),
        axis_name='batch',
        donate_argnums=(0, 1))
    pmapped_standard_train_step = jax.pmap(
        functools.partial(
            training_core.train_step,
            learning_rate_fn=learning_rate_fn,
            l2_reg=FLAGS.config.l2_regularization,
            r = 0.0,
            apply_fn = model.apply),
        axis_name='batch',
        donate_argnums=(0, 1)) if FLAGS.config.use_hybrid_training or FLAGS.config.warmup_epochs > 0 else None
    pmapped_eval_step = jax.pmap(
        functools.partial(
            training_core.eval_step,
            apply_fn = model.apply),
         axis_name='batch')

    # Start training.
    for epochs_id in range(initial_epoch, num_epochs):
        tick = time.time()

        # Train one epoch.
        logging.info(f"********************* Epoch {epochs_id} / {num_epochs} *********************")
        info = f"[Epoch {epochs_id}] Training the {epochs_id}th epoch. Please wait..."
        logging.info(info)
        optimizer, state, train_summary = training_core.train_for_one_epoch(
                optimizer, state, dataset_source, prng_key, epochs_id,
                pmapped_train_step,
                pmapped_standard_train_step,
                generate_scheduling_probability
            )
        # Write to tensorboard.
        current_step = int(optimizer.state.step[0])
        for metric_name, metric_value in train_summary.items():
            summary_writer.scalar(metric_name, metric_value, current_step)
        summary_writer.flush()
        tock = time.time()
        info = f"[Epoch {epochs_id}] Training complete! Totally cost {(tock - tick):.4f} seconds."
        logging.info(info)
        info = f"[Epoch {epochs_id}] Training Metric Summaries : "
        logging.info(info)
        info = f"[Epoch {epochs_id}]   -- Gradient Norm : {train_summary['gradient_norm']:.4f}"
        logging.info(info)
        info = f"[Epoch {epochs_id}]   -- Training loss : {train_summary['train_loss']:.4f}"
        logging.info(info)
        info = f"[Epoch {epochs_id}]   -- Training error rate : {train_summary['train_error_rate']:.4f}"
        logging.info(info)

        # Evaluate the model on the test set.
        if (epochs_id + 1) % FLAGS.config.evaluate_every_n_epochs == 0:
            info = f'[Epoch {epochs_id}] Evaluating at end of {epochs_id}th epoch'
            logging.info(info)
            tick = time.time()
            current_step = int(optimizer.state.step[0])
            test_ds = dataset_source.get_test()
            test_metrics = training_core.eval_on_dataset(
                optimizer.target, state, test_ds, pmapped_eval_step)
            for metric_name, metric_value in test_metrics.items():
                summary_writer.scalar('test_' + metric_name,
                                    metric_value, current_step)
            summary_writer.flush()
            tock = time.time()
            info = f"[Epoch {epochs_id}] Evaluation complete! Totally cost {(tock - tick):.4f} seconds."
            logging.info(info)
            info = f"[Epoch {epochs_id}] Testing Metric Summaries : "
            logging.info(info)
            info = f"[Epoch {epochs_id}]   -- Testing loss : {test_metrics['loss']:.4f}"
            logging.info(info)
            info = f"[Epoch {epochs_id}]   -- Testing error rate : {test_metrics['error_rate']:.4f}"
            logging.info(info)

        # Save chekpoint every n epochs, and ignore the first epoch.
        # You could config the keep flag in the save_checkpoint function
        # to decide the maximum number of saved checkpoint, where the 
        # default number is 2.
        if epochs_id % FLAGS.config.save_ckpt_every_n_epochs == 0 and epochs_id != 0:
            utli.save_checkpoint(optimizer, state, checkpoint_dir, epochs_id)
            logging.info(f'[Epoch {epochs_id}]  Saved checkpoint.')

    # Always save last checkpoint.
    utli.save_checkpoint(optimizer, state, checkpoint_dir, epochs_id)
