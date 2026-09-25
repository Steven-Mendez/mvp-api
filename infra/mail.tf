# The invitation sender. SES emails a verification link to this address on the first
# apply; until the account leaves the SES sandbox, only verified recipients get mail
# (the invitation link is also in the API answer, so invites work either way).
resource "aws_sesv2_email_identity" "sender" {
  email_identity = var.mail_from
}
