terraform {
  required_version = ">= 1.9.0" # validations that refer to other variables

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
  # How often the SDK retries one API call. EC2 answers
  # InsufficientInstanceCapacity with HTTP 500, which the SDK retries; the
  # provider default (25) keeps RunInstances retrying for many minutes.
  max_retries = var.max_retries
}
