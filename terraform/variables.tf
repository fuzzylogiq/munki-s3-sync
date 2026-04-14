variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "storage_bucket_name" {
  description = "Name of the S3 bucket for content-addressable package storage"
  type        = string
}

variable "repo_bucket_name" {
  description = "Name of the S3 bucket for serving the built Munki repo (behind CloudFront)"
  type        = string
}

variable "github_org" {
  description = "GitHub organization or user that owns the repo"
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name (without org prefix)"
  type        = string
}

variable "cloudfront_price_class" {
  description = "CloudFront price class"
  type        = string
  default     = "PriceClass_100" # US, Canada, Europe
}

variable "munki_basic_auth_user" {
  description = "Basic auth username for Munki client access (optional, leave empty to disable)"
  type        = string
  default     = ""
}

variable "munki_basic_auth_password" {
  description = "Basic auth password for Munki client access (optional)"
  type        = string
  default     = ""
  sensitive   = true
}
