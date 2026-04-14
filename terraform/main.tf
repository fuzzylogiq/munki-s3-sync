terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# CloudFront requires ACM certs in us-east-1 — add a second provider if you
# need a custom domain with TLS. For the default *.cloudfront.net domain this
# isn't necessary.

data "aws_caller_identity" "current" {}

# --------------------------------------------------------------------------
# S3: storage bucket (content-addressable package store)
# --------------------------------------------------------------------------

resource "aws_s3_bucket" "storage" {
  bucket = var.storage_bucket_name
}

resource "aws_s3_bucket_versioning" "storage" {
  bucket = aws_s3_bucket.storage.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "storage" {
  bucket                  = aws_s3_bucket.storage.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "storage" {
  bucket = aws_s3_bucket.storage.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --------------------------------------------------------------------------
# S3: repo/serving bucket (standard Munki layout, fronted by CloudFront)
# --------------------------------------------------------------------------

resource "aws_s3_bucket" "repo" {
  bucket = var.repo_bucket_name
}

resource "aws_s3_bucket_public_access_block" "repo" {
  bucket                  = aws_s3_bucket.repo.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "repo" {
  bucket = aws_s3_bucket.repo.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# --------------------------------------------------------------------------
# CloudFront: serves the repo bucket to Munki clients
# --------------------------------------------------------------------------

resource "aws_cloudfront_origin_access_control" "repo" {
  name                              = "${var.repo_bucket_name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "repo" {
  enabled             = true
  default_root_object = ""
  price_class         = var.cloudfront_price_class
  comment             = "Munki repo - ${var.repo_bucket_name}"

  origin {
    domain_name              = aws_s3_bucket.repo.bucket_regional_domain_name
    origin_id                = "S3-repo"
    origin_access_control_id = aws_cloudfront_origin_access_control.repo.id
  }

  default_cache_behavior {
    target_origin_id       = "S3-repo"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 604800
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    # To use a custom domain, replace the above with:
    # acm_certificate_arn      = aws_acm_certificate.example.arn
    # ssl_support_method       = "sni-only"
    # minimum_protocol_version = "TLSv1.2_2021"
  }
}

# Allow CloudFront to read from the repo bucket
resource "aws_s3_bucket_policy" "repo" {
  bucket = aws_s3_bucket.repo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowCloudFrontOAC"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.repo.arn}/*"
        Condition = {
          StringEquals = {
            "AWS:SourceArn" = aws_cloudfront_distribution.repo.arn
          }
        }
      }
    ]
  })
}

# --------------------------------------------------------------------------
# GitHub Actions OIDC
# --------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

# --------------------------------------------------------------------------
# IAM: role assumed by GitHub Actions workflows
# --------------------------------------------------------------------------

resource "aws_iam_role" "github_actions" {
  name = "munki-s3-sync-github-actions"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/${var.github_repo}:*"
          }
        }
      }
    ]
  })
}

# Storage bucket: upload, download, verify
resource "aws_iam_role_policy" "storage_bucket" {
  name = "storage-bucket-access"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BucketLevel"
        Effect = "Allow"
        Action = [
          "s3:HeadBucket",
          "s3:ListBucket",
        ]
        Resource = aws_s3_bucket.storage.arn
      },
      {
        Sid    = "ObjectLevel"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectAttributes",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.storage.arn}/*"
      }
    ]
  })
}

# Repo bucket: aws s3 sync from build-and-sync workflow
resource "aws_iam_role_policy" "repo_bucket" {
  name = "repo-bucket-access"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BucketLevel"
        Effect = "Allow"
        Action = [
          "s3:ListBucket",
        ]
        Resource = aws_s3_bucket.repo.arn
      },
      {
        Sid    = "ObjectLevel"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.repo.arn}/*"
      }
    ]
  })
}
