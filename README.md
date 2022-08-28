# Randomized Sharpness-Aware Training for Boosting Computational Efficiency in Deep Learning (AAAI2023)

The project runs on JAX. The virtual env should be installed based on the requirements.txt in the first place. And one could launch the training via
***
    python -m rst.main.main --config=path-to-the-train-config-file --working_dir=path-to-the-result-folder
            --config.model.model_name=WideResNet_28_10 --config.use_hybrid_training=True 
            --config.hybrid_config.p=0.6 --config.schedule_function_type=constant ......
***
Please see the paper for more details of training and config file for more flags. If encountering any problems, contact us!

