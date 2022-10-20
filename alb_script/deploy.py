import boto3
import json
import yaml
import argparse
import os

from typing import List, Mapping


def find_instances(region: str, clusters: List[str]):
    """find all running instances that match any of the cluster ids provided

    Args:
        region (str): the aws region
        clusters (List[str]): _description_

    Returns:
        instance_ids (List[str]): list of all instance ids that match any of the cluster ids provided
    """
    print(f"Searching for all instances with these cluster ids: {clusters} ...")
    client = boto3.client("ec2", region)
    tags = [
        {"Name": "tag:anyscale-session-id", "Values": clusters},
        {"Name": "instance-state-name", "Values": ["running"]},
    ]
    response = client.describe_instances(Filters=tags)
    instance_ids = []
    for reservation in response["Reservations"]:
        for instance in reservation["Instances"]:
            instance_ids.append(instance["InstanceId"])
    print(f"Found {len(instance_ids)} instances with these cluster ids: {clusters}.\n")
    return instance_ids


def parse_input(config_path):
    """parse input yaml and return a stack name, versions array, and other cloudformation parameters


    Args:
        config_path (str): path to the input yaml

    Returns:
        region (str): the aws region
        stack_name (str): name of the cloudformation stack
        versions (List[Tuple]): array containing information about each version of the deployed service
        parameters (dict): cloudformation parameters
    """
    print("Parsing the config yaml ...")
    inputs = yaml.safe_load(open(config_path, "r"))

    stack_name = inputs["stack_name"]
    region = inputs["region"]

    versions = []
    for version in inputs["versions"]:
        versions.append(version.values())

    parameters = [
        {
            "ParameterKey": "SecurityGroups",
            "ParameterValue": inputs["security_groups"],
        },
        {
            "ParameterKey": "Subnets",
            "ParameterValue": inputs["subnets"],
        },
        {"ParameterKey": "VPCID", "ParameterValue": inputs["vpc_id"]},
    ]
    print("Finished parsing the config yaml.\n")
    return region, stack_name, versions, parameters


def plan(region, stack_name, versions):
    """generate the cloudformation stack template with the given parameters

    Args:
        region (str): the aws region
        stack_name (str): name of the cloudformation stack
        versions: array containing information about each version of the deployed service
                - each version is defined with the following tuple (version, weight, [cluster_id, ...])

    Returns:
        cf_template (str): the generated cloudformation template in json form
    """
    print("Generating the cloudformation template ...\n")
    params = {
        "SecurityGroups": {
            "Description": "List of security groups",
            "Type": "CommaDelimitedList",
        },
        "Subnets": {
            "Description": "List of subnets that your instances reside in",
            "Type": "CommaDelimitedList",
        },
        "VPCID": {"Description": "VPC ID of all of the instances", "Type": "String"},
    }

    resources = {}
    resources[f"ALB{stack_name}"] = {
        "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "Properties": {
            "IpAddressType": "ipv4",
            "Name": f"ALB{stack_name}",
            "SecurityGroups": {"Ref": "SecurityGroups"},
            "Subnets": {"Ref": "Subnets"},
            "Type": "application",
        },
    }

    # generate default routing rule for the ALB dependent on given input weights
    target_groups = []
    for version, weight, _ in versions:
        target_groups.append(
            {"TargetGroupArn": {"Ref": f"TG{version}{stack_name}"}, "Weight": weight}
        )
    resources[f"ALBListener{stack_name}"] = {
        "Type": "AWS::ElasticLoadBalancingV2::Listener",
        "Properties": {
            "LoadBalancerArn": {"Ref": f"ALB{stack_name}"},
            "Port": 80,
            "Protocol": "HTTP",
            "DefaultActions": [
                {
                    "Type": "forward",
                    "ForwardConfig": {"TargetGroups": target_groups},
                }
            ],
        },
    }

    # generate target groups for each version of the service
    for version, weight, clusters in versions:
        targets = []
        for instance in find_instances(region, clusters):
            targets.append({"Id": instance, "Port": 8000})
        resources[f"TG{version}{stack_name}"] = {
            "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
            "Properties": {
                "HealthCheckIntervalSeconds": 5,
                "HealthCheckTimeoutSeconds": 4,
                "HealthyThresholdCount": 2,
                "HealthCheckPath": "/healthcheck",
                "Name": f"tg-{version}-{stack_name}",
                "Port": 8000,
                "Protocol": "HTTP",
                "ProtocolVersion": "HTTP1",
                "VpcId": {"Ref": "VPCID"},
                "Targets": targets,
            },
        }

    cf_template = {"Parameters": params, "Resources": resources}
    cf_template_json = json.dumps(cf_template, indent=2)

    with open("cf_template.json", "w") as file:
        file.write(cf_template_json)
    print("Generated the cloudformation template. A copy has been saved in cf_template.json.\n")
    return cf_template_json


def apply(region, stack_name, cf_template, parameters):
    """deploy the given cloudformation template

    Args:
        region (str): the aws region
        stack_name (str): name of the cloudformation stack
        cf_template (str): the generated cloudformation template in json form
        parameters (dict): cloudformation parameters
    """
    print("Applying the cloudformation template ...\n")
    client = boto3.client("cloudformation", region)
    try:
        client.create_stack(StackName=stack_name, TemplateBody=cf_template, Parameters=parameters)
        waiter = client.get_waiter("stack_create_complete")
        waiter_config = {}
    except client.exceptions.AlreadyExistsException:
        client.update_stack(
            StackName=stack_name,
            TemplateBody=cf_template,
            Parameters=parameters,
        )
        waiter = client.get_waiter("stack_update_complete")
        waiter_config = {"Delay": 2}

    stack_description = client.describe_stacks(StackName=stack_name)
    stack_id = stack_description["Stacks"][0]["StackId"]
    stack_url = f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}#stacks/stackinfo?stackId={stack_id}"
    print(f"View your stack at {stack_url}.\n")

    waiter.wait(StackName=stack_name, WaiterConfig=waiter_config)
    print("Finished deploying the cloudformation template.\n")
    alb_description = client.describe_stack_resource(
        StackName=stack_name, LogicalResourceId=f"ALB{stack_name}"
    )
    client = boto3.client("elbv2", "us-west-2")
    response = client.describe_load_balancers(
        LoadBalancerArns=[alb_description["StackResourceDetail"]["PhysicalResourceId"]]
    )

    ALB_dns_name = response["LoadBalancers"][0]["DNSName"]
    print(f"The DNS name of your ALB is {ALB_dns_name}.")


def delete(region, stack_name):
    """delete the specified stack

    Args:
        region (str): the aws region
        stack_name (str): name of the cloudformation stack
    """
    print(f"Deleting cloudformation stack {stack_name} ...")
    client = boto3.client("cloudformation", region)
    client.delete_stack(StackName=stack_name)

    waiter = client.get_waiter("stack_delete_complete")
    waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 1})
    print(f"Deleted cloudformation stack {stack_name}.")


parser = argparse.ArgumentParser(description="Apply the input yaml to a cloudformation stack")
parser.add_argument("verb", metavar="verb", type=str, help="Either apply or delete")
parser.add_argument(
    "path",
    metavar="config_path",
    type=os.path.abspath,
    help="The relative path to the config yaml",
)

if __name__ == "__main__":
    args = parser.parse_args()
    verb = args.verb
    config_path = args.path

    if verb not in ["apply", "delete"]:
        print(f"{verb} is not a valid verb. Please use either 'apply' or 'delete'.")
        quit()

    if not os.path.exists(config_path):
        print(f"The path specified ({config_path}) does not exist")
        quit()
    region, stack_name, versions, parameters = parse_input(config_path)

    if verb == "apply":
        template = plan(region, stack_name, versions)
        apply(region, stack_name, template, parameters)
    elif verb == "delete":
        delete(region, stack_name)
