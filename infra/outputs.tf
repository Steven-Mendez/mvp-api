# What the web and mobile apps (and CI) need.

output "api_url" {
  value = local.api_url
}

output "media_base_url" {
  value = local.media_url
}

output "region" {
  value = local.region
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "web_client_id" {
  value = aws_cognito_user_pool_client.app["web"].id
}

output "mobile_client_id" {
  value = aws_cognito_user_pool_client.app["mobile"].id
}

output "appsync_http_domain" {
  value = aws_appsync_api.events.dns["HTTP"]
}

output "appsync_realtime_domain" {
  value = aws_appsync_api.events.dns["REALTIME"]
}

output "dsql_endpoint" {
  value = local.dsql_endpoint
}
