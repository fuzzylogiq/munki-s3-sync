output "storage_bucket_name" {
  description = "Storage bucket name (set as S3_STORAGE_BUCKET GitHub secret)"
  value       = aws_s3_bucket.storage.id
}

output "repo_bucket_name" {
  description = "Repo bucket name (set as S3_REPO_BUCKET GitHub secret)"
  value       = aws_s3_bucket.repo.id
}

output "github_actions_role_arn" {
  description = "IAM role ARN (set as AWS_ROLE_ARN GitHub secret)"
  value       = aws_iam_role.github_actions.arn
}

output "cloudfront_distribution_domain" {
  description = "CloudFront domain name — point Munki clients here"
  value       = aws_cloudfront_distribution.repo.domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID (for cache invalidation if needed)"
  value       = aws_cloudfront_distribution.repo.id
}
