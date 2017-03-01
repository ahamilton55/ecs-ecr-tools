#!/usr/bin/env python2.7

import boto3
import json
import logging
import os
import time


if 'DRYRUN' in os.environ and bool(os.environ['DRYRUN']):
  dryrun=True
else:
  dryrun=False

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def check_instance_in_elbs_and_tgs(sess, instance_id, elbs, tgs):
  attempts = 0
  completed = False
  elb_client = sess.client('elb')
  elbv2_client = sess.client('elbv2')

  while not completed and attempts <= 5:
    completed = True

    for elb in elbs:
      health = elb_client.describe_instance_health(LoadBalancerName=elb,
                                        Instances=[{'InstanceId': instance_id}])
      logger.info("ELB: Id: {id}, state: {state}".format(id=instance_id, state=health['InstanceStates'][0]['State']))
      if health['InstanceStates'][0]['State'] == 'InService':
        completed = False

    for tg in tgs:
      health_info = elbv2_client.describe_target_health(TargetGroupArn=tg)
      for target in health_info['TargetHealthDescriptions']:
        if target['Target']['Id'] == instance_id:
          logger.info("TG: Id: {id}, state: {state}".format(id=instance_id,
                                         state=target['TargetHealth']['State']))
          if target['TargetHealth']['State'] in ('healthy', 'draining'):
            completed=False

    if not completed:
      time.sleep(5)
      attempts += 1

  return completed


def find_container_instance_in_cluster(sess, cluster_arn, instance_id):
  ecs_client = sess.client('ecs')

  container_instances = ecs_client.list_container_instances(cluster=cluster_arn)

  if len(container_instances['containerInstanceArns']) > 0:
    instance_info = ecs_client.describe_container_instances(cluster=cluster_arn,
                containerInstances=container_instances['containerInstanceArns'])

    for instance in instance_info['containerInstances']:
      if instance['ec2InstanceId'] == instance_id:
        return instance['containerInstanceArn']

  return None


def deregister_instance_from_cluster(sess, cluster, instance_id):
  ecs_client = sess.client('ecs')

  logger.info(instance_id)
  container_instance = find_container_instance_in_cluster(sess, cluster, instance_id)

  resp = ecs_client.deregister_container_instance(cluster=cluster,
                                                  containerInstance=container_instance,
                                                  force=True)


def find_drain_timings(sess, elbs, tgs):
  max_timeout = 0
  elb_client = sess.client('elb')

  for elb in elbs:
    info = elb_client.describe_load_balancer_attributes(LoadBalancerName=elb)
    if info['LoadBalancerAttributes']['ConnectionDraining']['Enabled']:
      if info['LoadBalancerAttributes']['ConnectionDraining']['Timeout'] > max_timeout:
        max_timeout = info['LoadBalancerAttributes']['ConnectionDraining']['Timeout']

  elbv2_client = sess.client('elbv2')
  for tg in tgs:
    tg_attrs = elbv2_client.describe_target_group_attributes(TargetGroupArn=tg)
    for attr in tg_attrs['Attributes']:
      if attr['Key'] == 'deregistration_delay.timeout_seconds':
        if max_timeout < int(attr['Value']):
          max_timeout = int(attr['Value'])

  return max_timeout


def find_elbs_and_tgs_for_services(services):
  elbs = []
  tgs = []

  for key in services:
    for elb in services[key]['loadBalancers']:
      if 'loadBalancerName' in elb:
        elbs.append(elb['loadBalancerName'])
      elif 'targetGroupArn' in elb:
        tgs.append(elb['targetGroupArn'])

  return (elbs, tgs)

def find_services_for_tasks(sess, cluster_arn, tasks):
  ecs_client = sess.client('ecs')

  tasks_info = ecs_client.describe_tasks(cluster=cluster_arn, tasks=tasks)

  task_definitions = []
  for task in tasks_info['tasks']:
    if not task['taskDefinitionArn'] in task_definitions:
      task_definitions.append(task['taskDefinitionArn'])

  cluster_services = ecs_client.list_services(cluster=cluster_arn)
  if len(cluster_services) == 0:
    return None

  services_info = ecs_client.describe_services(cluster=cluster_arn,
                                      services=cluster_services['serviceArns'])
  services = dict()
  for service in services_info['services']:
    if service['taskDefinition'] in task_definitions:
      arn = service['serviceArn']
      if not arn in services:
        services[arn] = service

  return services


def find_running_tasks(sess, cluster_arn, container_instance_arn):
  ecs_client = sess.client('ecs')

  tasks = ecs_client.list_tasks(cluster=cluster_arn,
                                containerInstance=container_instance_arn)

  return tasks['taskArns']


def get_cluster_for_instance(sess, instance_id):
  ecs_client = sess.client('ecs')

  clusters = ecs_client.list_clusters()

  found = False
  cluster_arn = ''
  container_instance_arn = ''

  for arn in clusters['clusterArns']:
    cluster_arn = arn
    container_instance_arn = find_container_instance_in_cluster(sess,
                                                                cluster_arn,
                                                                instance_id)
    if container_instance_arn:
      found = True
      break

  if found:
    return (cluster_arn, container_instance_arn)
  else:
    return (None, None)

def drain_instance(sess, instance_id):
  cluster_arn, container_instance_arn = get_cluster_for_instance(sess, instance_id)

  logger.info("cluster: {}".format(cluster_arn))
  logger.info("container instance arn: {}".format(container_instance_arn))

  task_arns = find_running_tasks(sess, cluster_arn, container_instance_arn)
  if len(task_arns) == 0:
    logger.info("Found no tasks!")
    return

  logger.info("Tasks: {}".format(task_arns))

  services = find_services_for_tasks(sess, cluster_arn, task_arns)

  elb_names, tg_arns = find_elbs_and_tgs_for_services(services)
  logger.info("ELBs: {}".format(elb_names))
  logger.info("TGs: {}".format(tg_arns))

  max_timeout = find_drain_timings(sess, elb_names, tg_arns)
  logger.info("Found max draining time: {}".format(max_timeout))

  if not dryrun:
    logger.info("degregistering instance from cluster")
    deregister_instance_from_cluster(sess, cluster_arn, instance_id)

    logger.info("Sleeping for {} seconds".format(max_timeout))
    time.sleep(max_timeout)

  logger.info("Checking that instance has been removed from ELBs and TGs")
  complete = check_instance_in_elbs_and_tgs(sess, instance_id, elb_names, tg_arns)


def setup_logger(instance_id, message=None):
  if message:
    format = 'timestamp=%(asctime)s lvl=%(levelname)s host={instance_id} asg={asg} hook={hook} msg=\'%(message)s\''.format(
                                instance_id=instance_id,
                                asg=message['AutoScalingGroupName'],
                                hook=message['LifecycleHookName'])
  else:
    format = 'timestamp=%(asctime)s lvl=%(levelname)s host=localhost msg=\'%(message)s\''.format(
                                instance_id=instance_id)

  formatter = logging.Formatter(fmt=format, datefmt='%Y-%m-%dT%H:%M:%SZ')
  sh = logging.StreamHandler()
  sh.setFormatter(formatter)
  logger.addHandler(sh)


def handler(event, context):
  logger.info(event)
  message = json.loads(event['Records'][0]['Sns']['Message'])
  setup_logger(message['EC2InstanceId'], message)
  logger.info(message)

  sess = boto3.Session()

  drain_instance(sess, message['EC2InstanceId'])

  if not dryrun:
    logger.info("Completing autoscaling action")
    as_client = sess.client('autoscaling')
    as_client.complete_lifecycle_action(
                           LifecycleHookName=message['LifecycleHookName'],
                           AutoScalingGroupName=message['AutoScalingGroupName'],
                           LifecycleActionToken=message['LifecycleActionToken'],
                           LifecycleActionResult='CONTINUE',
                           InstanceId=message['EC2InstanceId'])


if __name__ == "__main__":
  sess = boto3.Session(profile_name='vmnetops')

  # Instance ID with come from the event in the handler
  # Here I'm just using one that is currently setup to work
  instance_id = 'i-0c315bd8daf18cf20' #Five tasks
  #instance_id = 'i-06f5db3fb96083d98' #Empty
  instance_id = 'i-02b605a5d66793f27'

  setup_logger(instance_id)

  drain_instance(sess, instance_id)


