u"""
The ``fine-tune`` subcommand is used to continue training (or `fine-tune`) a model on a `different
dataset` than the one it was originally trained on.  It requires a saved model archive file, a path
to the data you will continue training with, and a directory in which to write the results.

Run ``allennlp fine-tune --help`` for detailed usage information.
"""


from __future__ import with_statement
from __future__ import absolute_import
import argparse
import json
import logging
import os
from copy import deepcopy
import re

from allennlp.commands.evaluate import evaluate
from allennlp.commands.subcommand import Subcommand
from allennlp.commands.train import datasets_from_params
from allennlp.common import Params
from allennlp.common.util import prepare_environment, prepare_global_logging,\
                                 get_frozen_and_tunable_parameter_names
from allennlp.data.iterators.data_iterator import DataIterator
from allennlp.models import load_archive, archive_model
from allennlp.models.archival import CONFIG_NAME
from allennlp.models.model import Model, _DEFAULT_WEIGHTS
from allennlp.training.trainer import Trainer
from allennlp.common.checks import ConfigurationError
from io import open

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

class FineTune(Subcommand):
    def add_subparser(self, name     , parser                            )                           :
        # pylint: disable=protected-access
        description = u"""Continues training a saved model on a new dataset."""
        subparser = parser.add_parser(name,
                                      description=description,
                                      help=u'Continue training a model on a new dataset')

        subparser.add_argument(u'-m', u'--model-archive',
                               required=True,
                               type=unicode,
                               help=u'path to the saved model archive from training on the original data')

        subparser.add_argument(u'-c', u'--config-file',
                               required=True,
                               type=unicode,
                               help=u'configuration file to use for training. Format is the same as '
                               u'for the "train" command, but the "model" section is ignored.')

        subparser.add_argument(u'-s', u'--serialization-dir',
                               required=True,
                               type=unicode,
                               help=u'directory in which to save the fine-tuned model and its logs')

        subparser.add_argument(u'-o', u'--overrides',
                               type=unicode,
                               default=u"",
                               help=u'a JSON structure used to override the training configuration '
                               u'(only affects the config_file, _not_ the model_archive)')

        subparser.add_argument(u'--extend-vocab',
                               action=u'store_true',
                               default=False,
                               help=u'if specified, we will use the instances in your new dataset to '
                                    u'extend your vocabulary. Currently expansion of embedding layers '
                                    u'is not implemented, so if your model has an embedding layer '
                                    u'this will probably make fine-tune crash.')

        subparser.add_argument(u'--file-friendly-logging',
                               action=u'store_true',
                               default=False,
                               help=u'outputs tqdm status on separate lines and slows tqdm refresh rate')

        subparser.set_defaults(func=fine_tune_model_from_args)

        return subparser


def fine_tune_model_from_args(args                    ):
    u"""
    Just converts from an ``argparse.Namespace`` object to string paths.
    """
    fine_tune_model_from_file_paths(model_archive_path=args.model_archive,
                                    config_file=args.config_file,
                                    serialization_dir=args.serialization_dir,
                                    overrides=args.overrides,
                                    extend_vocab=args.extend_vocab,
                                    file_friendly_logging=args.file_friendly_logging)


def fine_tune_model_from_file_paths(model_archive_path     ,
                                    config_file     ,
                                    serialization_dir     ,
                                    overrides      = u"",
                                    extend_vocab       = False,
                                    file_friendly_logging       = False)         :
    u"""
    A wrapper around :func:`fine_tune_model` which loads the model archive from a file.

    Parameters
    ----------
    model_archive_path : ``str``
        Path to a saved model archive that is the result of running the ``train`` command.
    config_file : ``str``
        A configuration file specifying how to continue training.  The format is identical to the
        configuration file for the ``train`` command, but any contents in the ``model`` section is
        ignored (as we are using the provided model archive instead).
    serialization_dir : ``str``
        The directory in which to save results and logs. We just pass this along to
        :func:`fine_tune_model`.
    overrides : ``str``
        A JSON string that we will use to override values in the input parameter file.
    file_friendly_logging : ``bool``, optional (default=False)
        If ``True``, we make our output more friendly to saved model files.  We just pass this
        along to :func:`fine_tune_model`.
    """
    # We don't need to pass in `cuda_device` here, because the trainer will call `model.cuda()` if
    # necessary.
    archive = load_archive(model_archive_path)
    params = Params.from_file(config_file, overrides)
    return fine_tune_model(model=archive.model,
                           params=params,
                           serialization_dir=serialization_dir,
                           extend_vocab=extend_vocab,
                           file_friendly_logging=file_friendly_logging)


def fine_tune_model(model       ,
                    params        ,
                    serialization_dir     ,
                    extend_vocab       = False,
                    file_friendly_logging       = False)         :
    u"""
    Fine tunes the given model, using a set of parameters that is largely identical to those used
    for :func:`~allennlp.commands.train.train_model`, except that the ``model`` section is ignored,
    if it is present (as we are already given a ``Model`` here).

    The main difference between the logic done here and the logic done in ``train_model`` is that
    here we do not worry about vocabulary construction or creating the model object.  Everything
    else is the same.

    Parameters
    ----------
    archive : ``Archive``
        A saved model archive that is the result of running the ``train`` command.
    train_data_path : ``str``
        Path to the training data to use for fine-tuning.
    serialization_dir : ``str``
        The directory in which to save results and logs.
    validation_data_path : ``str``, optional
        Path to the validation data to use while fine-tuning.
    extend_vocab: ``bool``, optional (default=False)
        If ``True``, we use the new instances to extend your vocabulary.
    file_friendly_logging : ``bool``, optional (default=False)
        If ``True``, we add newlines to tqdm output, even on an interactive terminal, and we slow
        down tqdm's output to only once every 10 seconds.
    """
    prepare_environment(params)
    if os.path.exists(serialization_dir) and os.listdir(serialization_dir):
        raise ConfigurationError("Serialization directory ({serialization_dir}) "
                                 "already exists and is not empty.")

    os.makedirs(serialization_dir, exist_ok=True)
    prepare_global_logging(serialization_dir, file_friendly_logging)

    serialization_params = deepcopy(params).as_dict(quiet=True)
    with open(os.path.join(serialization_dir, CONFIG_NAME), u"w") as param_file:
        json.dump(serialization_params, param_file, indent=4)

    if params.pop(u'model', None):
        logger.warning(u"You passed parameters for the model in your configuration file, but we "
                       u"are ignoring them, using instead the model parameters in the archive.")

    vocabulary_params = params.pop(u'vocabulary', {})
    if vocabulary_params.get(u'directory_path', None):
        logger.warning(u"You passed `directory_path` in parameters for the vocabulary in "
                       u"your configuration file, but it will be ignored. ")

    all_datasets = datasets_from_params(params)
    vocab = model.vocab

    if extend_vocab:
        datasets_for_vocab_creation = set(params.pop(u"datasets_for_vocab_creation", all_datasets))

        for dataset in datasets_for_vocab_creation:
            if dataset not in all_datasets:
                raise ConfigurationError("invalid 'dataset_for_vocab_creation' {dataset}")

        logger.info(u"Extending model vocabulary using %s data.", u", ".join(datasets_for_vocab_creation))
        vocab.extend_from_instances(vocabulary_params,
                                    (instance for key, dataset in list(all_datasets.items())
                                     for instance in dataset
                                     if key in datasets_for_vocab_creation))

    vocab.save_to_files(os.path.join(serialization_dir, u"vocabulary"))

    iterator = DataIterator.from_params(params.pop(u"iterator"))
    iterator.index_with(model.vocab)

    train_data = all_datasets[u'train']
    validation_data = all_datasets.get(u'validation')
    test_data = all_datasets.get(u'test')

    trainer_params = params.pop(u"trainer")
    no_grad_regexes = trainer_params.pop(u"no_grad", ())
    for name, parameter in model.named_parameters():
        if any(re.search(regex, name) for regex in no_grad_regexes):
            parameter.requires_grad_(False)

    frozen_parameter_names, tunable_parameter_names =\
                   get_frozen_and_tunable_parameter_names(model)
    logger.info(u"Following parameters are Frozen  (without gradient):")
    for name in frozen_parameter_names:
        logger.info(name)
    logger.info(u"Following parameters are Tunable (with gradient):")
    for name in tunable_parameter_names:
        logger.info(name)

    trainer = Trainer.from_params(model,
                                  serialization_dir,
                                  iterator,
                                  train_data,
                                  validation_data,
                                  trainer_params)

    evaluate_on_test = params.pop_bool(u"evaluate_on_test", False)
    params.assert_empty(u'base train command')
    try:
        metrics = trainer.train()
    except KeyboardInterrupt:
        # if we have completed an epoch, try to create a model archive.
        if os.path.exists(os.path.join(serialization_dir, _DEFAULT_WEIGHTS)):
            logging.info(u"Fine-tuning interrupted by the user. Attempting to create "
                         u"a model archive using the current best epoch weights.")
            archive_model(serialization_dir, files_to_archive=params.files_to_archive)
        raise

    # Now tar up results
    archive_model(serialization_dir, files_to_archive=params.files_to_archive)

    if test_data and evaluate_on_test:
        test_metrics = evaluate(model, test_data, iterator, cuda_device=trainer._cuda_devices[0])  # pylint: disable=protected-access
        for key, value in list(test_metrics.items()):
            metrics[u"test_" + key] = value

    elif test_data:
        logger.info(u"To evaluate on the test set after training, pass the "
                    u"'evaluate_on_test' flag, or use the 'allennlp evaluate' command.")

    metrics_json = json.dumps(metrics, indent=2)
    with open(os.path.join(serialization_dir, u"metrics.json"), u"w") as metrics_file:
        metrics_file.write(metrics_json)
    logger.info(u"Metrics: %s", metrics_json)

    return model
