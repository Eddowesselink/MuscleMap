#!/usr/bin/env python
# -*- coding: utf-8 -*-

# For usage, type: python mm_segment.py -h

# Authors: Richard Yin, Eddo Wesselink, and Kenneth Weber

# IMPORTS: necessary libraries, modules, including MONAI for image processing, argparse, and torch for Deep Learning
import argparse
import logging
import os
import sys
import glob
from monai.inferers import SliceInferer
from monai.networks.nets import UNet
from monai.transforms import (
    AsDiscreted,
    Compose,
    Invertd,
    CropForegroundd,
    LoadImaged,
    Orientationd,
    Spacingd,
    EnsureTyped,
    NormalizeIntensityd,
    EnsureChannelFirstd,
    FillHolesd,
    SaveImaged,
    KeepLargestConnectedComponentd,
)
from monai.networks.layers import Norm
from monai.utils import set_determinism
from monai.data import Dataset, DataLoader, decollate_batch
from mm_segmentUtil import check_image_exists, get_model_and_config_paths, load_model_config, validate_arguments
import torch

# get_parser: parses command line arguments, sets up a) required (image, body region), and b) optional arguments (model, output file name, output directory)
def get_parser():
    parser = argparse.ArgumentParser(
        description="Segment an input image according to the specified deep learning model.")
    
    # Required arguments
    required = parser.add_argument_group("Required")
    required.add_argument("-i", '--image', required=True, type=str,
                          help="Image to segment. Can be multiple images separated with spaces.")
    required.add_argument("-r", '--region', required=True, type=str,
                          help="Body region of input image.")
    
    # Optional arguments
    optional = parser.add_argument_group("Optional")
    optional.add_argument("-m", '--model', default=None, required=False, type=str,
                          help="Option to specify another model.")
    optional.add_argument("-o", '--output_file_name', default='image_dseg.nii.gz', required=False, type=str,
                          help="Output file name. By default, dseg suffix will be added, and the output extension will be .nii.gz.")
    optional.add_argument("-s", '--output_dir', default='../output', required=False, type=str,
                          help="Output directory, default is the output folder")
    return parser

# main: sets up logging, parses command-line arguments using parser, runs model, inference, post-processing
def main():

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    parser = get_parser()
    args = parser.parse_args()

    # Validate Arguments
    validate_arguments(args)

    # Check that the image exists and is readable
    logging.info(f"Checking if image '{args.image}' exists and is readable...")
    check_image_exists(args.image)

    # Load model configuration
    logging.info("Loading configuration file...")

    # Get model and config paths
    model_path, model_config_path = get_model_and_config_paths(args.region, args.model)

    # Load model configuration
    model_config = load_model_config(model_config_path)

    try:
        roi_size = tuple(model_config['parameters']['roi_size'])
        spatial_window_batch_size = model_config['parameters']['spatial_window_batch_size']
        amount_of_labels = model_config['parameters']['amount_of_labels'] #not used
        pix_dim = tuple(model_config['parameters']['pix_dim'])
        model_continue_training = model_config['parameters']['model_continue_training'] #not used

        # Load model configuration parameters
        spatial_dims = model_config['model']['spatial_dims']
        in_channels = model_config['model']['in_channels']
        out_channels = model_config['model']['out_channels']
        channels = model_config['model']['channels']
        act = model_config['model']['act']
        strides = model_config['model']['strides']
        num_res_units = model_config['model']['num_res_units']
        num_labels = model_config['model']['num_labels']
    except KeyError as e:
        logging.error(f"Missing key in model configuration file: {e}")
        sys.exit(1)

    # Directory setup
    data_dir = args.image
    save_dir = os.path.abspath(args.output_dir)
    if not os.path.exists(save_dir):
        logging.info(f"Creating output directory at '{save_dir}'...")
        os.makedirs(save_dir)
    images = sorted(glob.glob(data_dir)) 
    test_files = [{"image": img} for img in images]

    # Use os.path.join for all path constructions to ensure cross-platform compatibility
    # Set seed for reproducibility (identical to training part)
    set_determinism(seed=0)  # Seed for reproducibility (identical to training part)

    # Create transforms identical to training part, but here we don't specify the label key
    inference_transforms = Compose([
        LoadImaged(keys=["image"]),
        EnsureChannelFirstd(keys=["image"]),
        Spacingd(
            keys=["image"],
            pixdim=pix_dim,
            mode=("bilinear")),
        Orientationd(keys=["image"], axcodes="RAS"),
        CropForegroundd(keys=["image"], source_key="image"),
        NormalizeIntensityd(keys=["image"], nonzero=True),
        EnsureTyped(keys=["image"])
    ])

    # Create iterable dataset and dataloader, identical to training part
    inference_transforms_dataset = Dataset(
        data=test_files, transform=inference_transforms,
    )

    inference_transforms_loader = DataLoader(
        inference_transforms_dataset, batch_size=1, shuffle=False, num_workers=1, pin_memory=True)

    # Device config
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Ensure flexibility to handle models with varying label counts
    # Validate post processing steps based on specific requirements of different models
    post_transforms = Compose([
        Invertd(
            keys="pred",
            transform=inference_transforms,
            orig_keys="image",
            meta_keys="pred_meta_dict",
            orig_meta_keys="image_meta_dict",
            meta_key_postfix="meta_dict",
            nearest_interp=False,
            to_tensor=True,
            device=device
        ),
        AsDiscreted(keys="pred", argmax=True),
        #add something here to customize based on label number

        FillHolesd(keys="pred", applied_labels=list(range(1, num_labels + 1))),  #dynamic num_labels
        KeepLargestConnectedComponentd(keys="pred", applied_labels=list(range(1, num_labels + 1))),  
        SaveImaged(keys="pred", meta_keys="pred_meta_dict", output_dir=save_dir, output_dtype=('int16'), separate_folder=False, resample=False)
    ])

    # Create model and set parameters
    model = UNet(
        spatial_dims=spatial_dims,
        in_channels=in_channels,
        out_channels=out_channels,
        channels=channels,
        act=act,
        strides=strides,
        num_res_units=num_res_units,
        norm=Norm.INSTANCE,
    ).to(device)

    # Load pre-existing model if we want to continue training
    map_location = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info(f"Loading model from '{model_path}'...")
    model.load_state_dict(torch.load(model_path, map_location=map_location))
    model.eval()

    # Run inference on the image and images using model, post-process the predictions
    with torch.no_grad():
        for i, input_data in enumerate(inference_transforms_loader):
            logging.info(f"Running inference on batch {i+1}/{len(inference_transforms_loader)}...")
            val_inputs = input_data["image"].to(device)
            axial_inferer = SliceInferer(roi_size=roi_size, sw_batch_size=spatial_window_batch_size, spatial_dim=2)
            input_data["pred"] = axial_inferer(val_inputs, model)
            val_data = [post_transforms(i) for i in decollate_batch(input_data)]
            logging.info(f"Inference and post-processing completed for batch {i+1}/{len(inference_transforms_loader)}.")
            logging.info(f"Saving output for batch {i+1}/{len(inference_transforms_loader)}...")

    logging.info("Inference completed. All outputs saved.")

if __name__ == "__main__":
    main()
