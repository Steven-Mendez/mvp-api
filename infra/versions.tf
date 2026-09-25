terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Partial configuration: the bucket, key and region are passed to `terraform init`
  # (`make tf-init`), so the repository names no account. S3-native locking, no
  # DynamoDB lock table.
  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "mvp-api"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
