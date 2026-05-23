import json
import os

import boto3


ecs = boto3.client("ecs")
elasticache = boto3.client("elasticache")


def handler(event, context):
    """
    Lambda handler for shutdown orchestration.

    This Lambda only issues shutdown commands. Verification, CloudWatch export,
    HTML report generation, and final report-ready email are handled after the
    verify_shutdown Lambda confirms cleanup.
    """

    cluster_id = os.environ["CLUSTER_ID"]
    ecs_cluster = os.environ["ECS_CLUSTER"]
    ecs_service = os.environ["ECS_SERVICE"]
    elasticache_id = os.environ["ELASTICACHE_ID"]

    results = {
        "cluster_id": cluster_id,
        "ecs_stopped": False,
        "elasticache_stopped": False,
    }

    try:
        ecs.update_service(
            cluster=ecs_cluster,
            service=ecs_service,
            desiredCount=0,
        )
        results["ecs_stopped"] = True
        print(f"ECS service {ecs_service} scaled to 0")
    except Exception as exc:
        print(f"ECS stop note: {exc}")
        results["ecs_stopped"] = str(exc)

    try:
        delete_params = {
            "ReplicationGroupId": elasticache_id,
            "RetainPrimaryCluster": False,
        }
        final_snapshot_id = os.environ.get("ELASTICACHE_FINAL_SNAPSHOT_ID")
        if final_snapshot_id:
            delete_params["FinalSnapshotIdentifier"] = final_snapshot_id

        elasticache.delete_replication_group(**delete_params)
        results["elasticache_stopped"] = True
        print(f"ElastiCache {elasticache_id} delete initiated")
    except Exception as exc:
        print(f"ElastiCache delete note: {exc}")
        results["elasticache_stopped"] = str(exc)

    print("Shutdown commands issued. Verification will handle export/report handoff.")
    return {
        "statusCode": 200,
        "body": json.dumps(results),
    }
