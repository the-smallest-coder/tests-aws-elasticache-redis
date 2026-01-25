# IAM Role for ECS Task Execution (pulling images, pushing logs)
resource "aws_iam_role" "ecs_task_execution_role" {
  name = "${local.cluster_id}-ecs-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.cluster_id}-ecs-execution"
  }
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution_role_policy" {
  role       = aws_iam_role.ecs_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# IAM Role for ECS Task (container's AWS API access)
resource "aws_iam_role" "ecs_task_role" {
  name = "${local.cluster_id}-ecs-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.cluster_id}-ecs-task"
  }
}

# Allow ECS task to write logs (in case execution role doesn't cover it)
resource "aws_iam_role_policy" "ecs_task_logs" {
  name = "${local.cluster_id}-ecs-task-logs"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "${aws_cloudwatch_log_group.loadgen.arn}:*"
      }
    ]
  })
}
# Report Generator Task Execution Role
resource "aws_iam_role" "report_task_execution_role" {
  name = "${local.cluster_id}-report-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.cluster_id}-report-execution"
  }
}

resource "aws_iam_role_policy_attachment" "report_task_execution_role_policy" {
  role       = aws_iam_role.report_task_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Report Generator Task Role (S3 Access)
resource "aws_iam_role" "report_task_role" {
  name = "${local.cluster_id}-report-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${local.cluster_id}-report-task"
  }
}

resource "aws_iam_role_policy" "report_task_policy" {
  name = "${local.cluster_id}-report-task-policy"
  role = aws_iam_role.report_task_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "arn:aws:s3:::${var.metrics_export_s3_bucket}/*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = "arn:aws:s3:::${var.metrics_export_s3_bucket}"
      }
    ]
  })
}
