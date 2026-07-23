from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import ECR, Fargate
from diagrams.aws.database import ElasticacheForRedis, RDS
from diagrams.aws.devtools import Codepipeline
from diagrams.aws.management import Cloudwatch
from diagrams.aws.network import ELB, InternetGateway
from diagrams.aws.security import SecretsManager

graph_attrs = {
    "fontsize": "13",
    "bgcolor": "white",
    "pad": "0.5",
    "splines": "ortho",
}

node_attrs = {"fontsize": "11"}

with Diagram(
    "GPU Index API - ECS Fargate, no NAT",
    filename="docs/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attrs,
    node_attr=node_attrs,
):
    igw = InternetGateway("internet")
    alb = ELB("ALB :80\nhealth check /readyz")

    with Cluster("public subnets (2 AZ)"):
        with Cluster("ECS Fargate service"):
            api = Fargate("gpu-index-api\n0.5 vCPU / 1 GB")

        redis = ElasticacheForRedis("ElastiCache Redis\ncache + rate limit")
        rds = RDS("RDS PostgreSQL 16\ndb.t4g.micro")

    secret = SecretsManager("Secrets Manager\nDB URL, Redis URL, API keys")
    ecr = ECR("ECR\nimage :sha")
    logs = Cloudwatch("CloudWatch Logs\n+ $10 budget alert")
    ci = Codepipeline("GitHub Actions\nOIDC, no static keys")

    igw >> alb
    alb >> Edge(label="8000, ALB SG only") >> api

    api >> Edge(label="read-through cache") >> redis
    api >> Edge(label="asyncpg") >> rds

    api >> Edge(label="at task start", style="dotted") >> secret
    api >> Edge(style="dotted") >> logs

    ci >> Edge(label="push image", color="darkgreen") >> ecr
    ecr >> Edge(label="pull", style="dashed") >> api
    ci >> Edge(label="smoke test, rollback on fail", color="firebrick") >> alb
