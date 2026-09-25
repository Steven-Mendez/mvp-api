locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  dsql_endpoint = "${aws_dsql_cluster.main.identifier}.dsql.${local.region}.on.aws"
  api_url       = "https://${aws_cloudfront_distribution.api.domain_name}"
  media_url     = "https://${aws_cloudfront_distribution.media.domain_name}"
}
