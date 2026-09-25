# AppSync Events. Each person subscribes to their own channel `/users/<sub>` with their
# Cognito access token; the API publishes over HTTP with its IAM role, once per member
# of the workspace a change belongs to. Billed per operation: nothing without traffic.
resource "aws_appsync_api" "events" {
  name = local.name

  event_config {
    auth_provider {
      auth_type = "AMAZON_COGNITO_USER_POOLS"
      cognito_config {
        user_pool_id = aws_cognito_user_pool.main.id
        aws_region   = local.region
      }
    }
    auth_provider {
      auth_type = "AWS_IAM"
    }

    connection_auth_mode {
      auth_type = "AMAZON_COGNITO_USER_POOLS"
    }
    default_publish_auth_mode {
      auth_type = "AWS_IAM"
    }
    default_subscribe_auth_mode {
      auth_type = "AMAZON_COGNITO_USER_POOLS"
    }
  }
}

resource "aws_appsync_channel_namespace" "users" {
  api_id = aws_appsync_api.events.api_id
  name   = "users"

  publish_auth_mode {
    auth_type = "AWS_IAM"
  }
  subscribe_auth_mode {
    auth_type = "AMAZON_COGNITO_USER_POOLS"
  }

  # Nobody listens on someone else's channel.
  code_handlers = <<-JS
    import { util } from '@aws-appsync/utils';

    export function onSubscribe(ctx) {
      if (ctx.info.channel.path !== `/users/$${ctx.identity.sub}`) {
        util.unauthorized();
      }
    }
  JS
}
