#!/usr/bin/env python

import argparse
import logging
import os
import sys
import time

import boto3

from ecsUpdate import ApplyECS

logging.basicConfig(format="%(asctime)s %(levelname)s [%(threadName)s] - %(message)s", stream=sys.stdout,
                    level=logging.INFO)


class ApplyCF:
    def __init__(self):
        self.success_status = ['CREATE_COMPLETE','UPDATE_COMPLETE']
        self.failed_status = ['CREATE_FAILED', 'ROLLBACK_IN_PROGRESS','ROLLBACK_FAILED','ROLLBACK_COMPLETE','UPDATE_ROLLBACK_IN_PROGRESS','UPDATE_ROLLBACK_FAILED','UPDATE_ROLLBACK_COMPLETE']

    def catfile(self, fn):
      with open(fn) as f:
        print f.read()

    def main(self, dir_, cf_params):

        file_dir = os.path.dirname(os.path.realpath(__file__))
        job_path = os.path.join(file_dir, dir_)

        self.load(job_path, cf_params)

    def load(self, job_path, cf_params):
        env = cf_params['env']
        cluster = cf_params['cluster']
        region = cf_params['region']
        elb_name = 'ecs-elb-' + cluster
        cf_client = boto3.client('cloudformation', region_name=region)

        self.extract_common_ecs_params(cf_params, cluster, elb_name, env, region)

        for subdir, dirs, files in os.walk(job_path):
            for fn in files:
                filename = os.path.join(subdir, fn)

                # Skip non-cf files
                ext = filename.split('.')[-1]
                if ext != 'template' and ext != 'yml':
                    continue
                name = filename.split('/')[-1].split('.')[0]
                cf_params['name'] = name
                logging.info("Processing CloudFormation Template: " + filename)
                parameters = [{'ParameterKey': 'name', 'ParameterValue': name}]

                if name is None or name in filename:
                    with open(filename, 'r') as f_h:
                        try:
                            cf_template = f_h.read()
                        except:
                            logging.exception("Error reading file %s " % filename)
                            self.catfile(filename)
                            raise
                        validate_response = {}
                        try:
                            validate_response = cf_client.validate_template(TemplateBody=cf_template)
                            logging.info("CloudFormation template validated")
                        except Exception as e:
                            logging.error("Error validating file: %s" % filename)
                            logging.error(validate_response)
                            logging.exception(e)
                            sys.exit(1)

                        try:
                            for cf_parameter in validate_response['Parameters']:
                                if cf_parameter['ParameterKey'] not in cf_params:
                                    logging.warning("Parameter: %s is specified by template in %s but not specified after --cfparams" % (filename, cf_parameter['ParameterKey']))
                                parameters.append({'ParameterKey': cf_parameter['ParameterKey'],
                                                   'ParameterValue': cf_params[cf_parameter['ParameterKey']]})
                            service_name = "%s-%s-%s" % (env, name, cluster)
                            existing_stacks = cf_client.list_stacks()
                            existing_stack_id = None
                            cf_command = cf_client.create_stack
                            for stack in existing_stacks['StackSummaries']:
                                if stack['StackName'] == service_name and stack['StackStatus'] != 'DELETE_COMPLETE':
                                    existing_stack_id = stack['StackId']
                                    cf_command = cf_client.update_stack
                                    break
                            logging.info("Updating CloudFormation Stack: " + service_name)
                            cf_response = cf_command(StackName=service_name, TemplateBody=cf_template, Parameters=parameters, Capabilities=["CAPABILITY_IAM"])
                            creating_stack_id = cf_response['StackId']
                            stack_status = self.wait_for_stack_creation(cf_client, creating_stack_id, service_name)
                            if existing_stack_id is not None:
                                print("Registering new task defintion to restart services/deploy new containers")
                                self.restart_tasks(cf_client, existing_stack_id, region, cf_params['cluster'])
                        except Exception as e:
                            logging.error("Error executing CloudFormation: %s" % filename)
                            logging.exception(e)
                            sys.exit(1)

    def restart_tasks(self, cf_client, existing_stack_id, region, cluster):
        stack_resources = cf_client.describe_stack_resources(StackName=existing_stack_id)
        for resource in stack_resources['StackResources']:
            if resource['ResourceType'] == "AWS::ECS::TaskDefinition":
                task_definition_arn = resource['PhysicalResourceId']
            if resource['ResourceType'] == "AWS::ECS::Service":
                service_arn = resource['PhysicalResourceId']

        ecs_client = boto3.client('ecs', region_name=region)
        describe_task_response = ecs_client.describe_task_definition(taskDefinition=task_definition_arn)
        new_task = describe_task_response['taskDefinition']
        new_task.pop('requiresAttributes')
        new_task.pop('revision')
        new_task.pop('status')
        new_task.pop('taskDefinitionArn')
        register_task_response = ecs_client.register_task_definition(**new_task)

        update_service_response = ecs_client.update_service(cluster=cluster, service=service_arn, taskDefinition=register_task_response['taskDefinition']['taskDefinitionArn'])
        update_status_code = update_service_response['ResponseMetadata']['HTTPStatusCode']
        print("Update complete, status code: %d" % update_status_code)
        if update_status_code >= 400:
            sys.exit(1)

    def wait_for_stack_creation(self, cf_client, creating_stack_id, service_name):
        while True:
            time.sleep(5)
            try:
                describe_stacks_response = cf_client.describe_stacks(StackName=creating_stack_id)
                stack_status = describe_stacks_response['Stacks'][0]['StackStatus']
                if stack_status in self.success_status:
                    logging.info("Stack update complete, status: %s" % stack_status)
                    break
                elif stack_status in self.failed_status:
                    logging.error("Stack update failed, status: %s" % stack_status)
                    sys.exit(1)
                else:
                    logging.info("Stack update in progress, status: %s" % stack_status)
            except Exception as e:
                logging.error("CloudFormation executed OK but stack was not created/updated: %s" % service_name)
                logging.exception(e)
                sys.exit(1)
        return stack_status

    def extract_common_ecs_params(self, cf_params, cluster, elb_name, env, region):
        elb_client = boto3.client('elbv2', region_name=region)
        balancer_arn, vpc_id = ApplyECS.get_load_balancer(elb_client, elb_name, cluster, env)
        listener_arn = ApplyECS.get_elb_listener(elb_client, balancer_arn)
        cf_params['vpcid'] = vpc_id
        cf_params['listenerarn'] = listener_arn
        response = elb_client.describe_rules(ListenerArn=listener_arn)
        rules = response['Rules']
        if 'priority' not in cf_params:
            top_priority = max([int(r['Priority']) if r['Priority'] != 'default' else 0 for r in rules])
            cf_params['priority'] = str(int(top_priority) + 1)


def argv_to_dict(args):
    argsdict = {}
    for farg in args:
        if farg.startswith('--'):
            key = farg[2:]
        else:
            value = farg
            argsdict[key] = value
    return argsdict

def validate_cf_params(cf_params):
    if 'env' not in cf_params:
        logging.error("--cfparams must contain --env [value]")
        sys.exit(1)
    if 'cluster' not in cf_params:
        logging.error("--cfparams must contain --cluster [value]")
        sys.exit(1)
    if 'region' not in cf_params:
        logging.error("--cfparams must contain --region [value]")
        sys.exit(1)

if __name__ == '__main__':
    p = ApplyCF()
    print(sys.argv)

    parser = argparse.ArgumentParser(description='Executes CloudFormation templates to create / update ECS related resources')
    parser.add_argument('--dir', help='relative directory name of service and task definitions', default='ecs')
    parser.add_argument('--cfparams', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cf_params = argv_to_dict(args.cfparams)
    validate_cf_params(cf_params)
    p.main(args.dir, cf_params)
