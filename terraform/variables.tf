variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the stack VPC."
  type        = string
  default     = "10.30.0.0/16"
}

variable "desired_count" {
  description = "Number of Fargate tasks. One is enough for a demo window."
  type        = number
  default     = 1
}

variable "container_image" {
  description = "Fully qualified image URI. Empty falls back to the ECR :latest tag."
  type        = string
  default     = ""
}

variable "budget_limit_usd" {
  description = "Monthly cost budget ceiling in USD. Guards against an orphaned deploy."
  type        = string
  default     = "10"
}

variable "budget_alert_email" {
  description = "Address that receives budget threshold notifications. Required: set via terraform.tfvars or TF_VAR_budget_alert_email."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be a valid email address."
  }
}
