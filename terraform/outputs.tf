output "budget_name" {
  description = "Name of the cost budget guarding this stack."
  value       = aws_budgets_budget.monthly_cost.name
}

output "api_base_url" {
  description = "Public base URL for the API."
  value       = "http://${aws_lb.main.dns_name}"
}

output "ecr_repository_url" {
  description = "Push target for the container image."
  value       = aws_ecr_repository.app.repository_url
}

output "api_key" {
  description = "Generated API key for X-API-Key."
  value       = random_password.api_key.result
  sensitive   = true
}

output "db_address" {
  description = "RDS endpoint, reachable only from the app security group."
  value       = aws_db_instance.main.address
}
