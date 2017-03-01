# AWS ECS Instance Drain Lambda Job

A AWS Lambda job for draining AWS Elastic Container Service instances behind ELBs and ALBs when scale-in events are triggered by AWS EC2 AutoScaling events.

The Lambda job will listen on AWS SNS for AWS AutoScaling scale-in (when instances are being removed or rotated) events to be sent. When the job is started, it looks up information about the instance and any running tasks. It then deregisters the instance from the ECS cluster and waits for a timeout period before checking to make sure that the instance is no longer behind the ELB or ALB.

## Setup

### Create an S3 bucket for the Lambda

Create a bucket in the same region that the Lambda job will be running and update the commands below with the name of the bucket.

### Update Cloudformation template

Update the Cloudformation template with information specific to the envirnoment. Information that needs to be updated includes:

* Lambda code bucket
* AWS EC2 AutoScaling event SNS topics for the Lambda job to watch. Make sure to add both a "Subscription" and "Execution" section for every topic this job should work against.

### Update AWS ECS AutoScaling group to use SNS for scale-in events

Th AWS AutoScaling group that the ECS cluster is running in needs to be setup to send SNS message on scale-in events.

The AWS AutoScaling group will need a LifeCycleHook for the terminating transition as well as an SNS topic that this job will connect to.

### Dependencies

The only dependency currently required is boto3 but that is automatically available in the Python2.7 AWS Lambda environment so it does not need to be packaged. There is a `requirements.txt` file but it currently is not required if editing this function.

### Dry Run

If you'd like to test this in Lambda without affecting your cluster you can set enter dry run mode. To enter this, add an environment variable called `DRYRUN` to any value and run the script. This will prevent de-registration of the container instance and will not signal the AutoScaling group to terminate the container instance.

## Upload the Lambda code to S3

To give the code to AWS Lambda, it should be uplaoded to AWS S3. Use the following steps in the current directory to upload the a zip file to a bucket named "MYBUCKET". You should replace "MYBUCKET" in the script below.

``` bash
$ zip instance_drain.zip instance_drain.py

$ aws s3 cp instance_drain.zip s3://MYBUCKET/instance_drain.zip
```

## Create the AWS lambda job using Cloudformation

### Create the stack the first run
```bash
$ aws cloudformation create-stack --stack-name instance-drain-lambda --template-body file://${PWD}/instance-drain-cf.yaml --capabilities CAPABILITY_IAM
```

### Update the stack for any additional updates

```bash
$ aws cloudformation update-stack --stack-name instance-drain-lambda --template-body file://${PWD}/instance-drain-cf.yaml --capabilities CAPABILITY_IAM
```

## Required IAM capabilities

The Lambda job CloudFormation template will add a role for the Lambda job to perform the tasks required.

The following capabilities are currently required:

* autoscaling:CompleteLifecycleAction
* ecs:DeregisterContainerInstance
* ecs:DescribeClusters
* ecs:DescribeContainerInstances
* ecs:DescribeServices
* ecs:DescribeTasks
* ecs:ListClusters
* ecs:ListContainerInstances
* ecs:ListServices
* ecs:ListTasks
* elasticloadbalancing:DescribeInstanceHealth
* elasticloadbalancing:DescribeTargetHealth
* elasticloadbalancing:DescribeLoadBalancerAttributes
* elasticloadbalancing:DescribeTargetGroupAttributes
* logs:*

The majority of the capabilities are for gathering information. The `ecs:DeregisterContainerInstance` and `autoscaling:CompleteLifecycleAction` are the two capabilities that will change state of the cluster. The former is to remove the instance from the cluster and to start draining of tasks while the latter is to signal AutoScaling that the task has completed and the instance can be terminated.

The `logs:*` permissions are to allow the lambda job to send logs to AWS Cloudwatch logs. This may be removed if you do not want logging.

## Limitations

### Tasks must be removed from the instance within 5 minutes to work properly

Due to the maximum run time of Lambda jobs, tasks must be evacuated within 5 minutes for the job to terminate the instance. If this doesn't happen, the node will be removed prematurely which could cause to loss of connections for users.

If draining tasks that last longer than 5 minutes, another solution will be required. Ideally this would be handled in the ECS container instance agent but I'm still not sure how to implement that.

### It currently doesn't use the DRAINING state for the container instance

This script was written before the DRAINING state was added to the AWS ECS container instance agent. Instead of setting the state to get the cluster to move things around, the script currently deregisters the container instance from the ECS cluster and then checks to make sure that the tasks have been removed from the ELB after the draining period. I believe that with the DRAINING state, the instance will acknowledge when the tasks have been moved and not require looking at the ELBs and ALBs.
