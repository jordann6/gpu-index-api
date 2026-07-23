resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "gpu-index-api"
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_db_instance" "main" {
  identifier = "gpu-index-api"
  engine     = "postgres"
  # Pin the minor version so a rebuild is reproducible. AWS retires older
  # minors, so this needs revisiting: 16.4 was already gone by deploy time.
  engine_version = "16.14"
  instance_class = "db.t4g.micro"

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "gpuindex"
  username = "gpu"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.data.id]
  publicly_accessible    = false

  # Ephemeral stack: no snapshot on destroy, no retained backups to orphan.
  skip_final_snapshot     = true
  backup_retention_period = 0
  deletion_protection     = false
  apply_immediately       = true

  performance_insights_enabled = false
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "gpu-index-api"
  subnet_ids = aws_subnet.public[*].id
}

resource "aws_elasticache_cluster" "main" {
  cluster_id           = "gpu-index-api"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.data.id]
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "gpu-index-api/runtime"
  recovery_window_in_days = 0 # Immediate delete so destroy is clean.
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    DATABASE_URL = "postgresql+asyncpg://gpu:${random_password.db.result}@${aws_db_instance.main.address}:5432/gpuindex"
    REDIS_URL    = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:6379/0"
    API_KEYS     = random_password.api_key.result
  })
}
