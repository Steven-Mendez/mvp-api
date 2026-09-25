# Aurora DSQL: serverless PostgreSQL-compatible, scales to zero, reached over IAM with no
# VPC (so no NAT gateway). The free tier covers an MVP's DPUs and storage.
resource "aws_dsql_cluster" "main" {
  deletion_protection_enabled = var.deletion_protection

  tags = {
    Name = local.name
  }
}
