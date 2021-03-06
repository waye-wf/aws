#!/usr/bin/env python3
#  aws_cleanup.py 
#  2018.06.12 - William Waye 
#  2018.06.24 - ww - consolidated code via ternary operators & added AWS Volume
#  2018.06.29 - ww - improved UI, added S3 bucket, removed blacklist (using "keep" tag instead).
#  2018.07.01 - ww - added POC code for warning about EC2 instances with dependency on
#                     Security Groups targeted for delete.
#  2018.07.09 - ww - added a couple traps for handling (potential) common errors for AWS connections.
#  2018.07.10 - ww - Changed delete verification from entering "yes" to a 4-digit delete
#     verification code, added termTrackClass (primarily for having variable constants instead of
#     string constants for dictionary indexes), and added "--region_test" argument (reduced number of regions
#     during dev testing for improved performance). Changed region break format.
#  2018.07.17 - ww - Added VPC, subnets, internet gateways, route tables. Added a couple
#     dependency warnings.
#  2018.07.23 - ww - Shifted variables that could be updated by end-user to external file
#                    aws_cleanup_import.py (could be convinced to place these variables back into
#                    this script). Corrected subnets order in deletion. Changed
#                    argument storage from class to namedtuple.
#  2018.07.30 - ww - Added user, group, policy, role AWS components.
#                    Included check to block connected user from being deleted.
#                    Added handling to bypass deleting "AWSServiceRoleForSupport".
#  2018.08.05 - ww - Added VPCEndpoints and default VPC delete/rebuild
#  2018.09.21 - ww - Added Metric Alarms, Config Rules, Configuration Recorder, Cloud Trail, Cloud Watch Log
#                    Group, SNS Topic. Updated script framework, format improvements, etc. Removed  
#                    option for deleting by specific tag - too many components with non-tags.
#  2018.09.25 - ww - Added CloudFormation Stacks, updated code for report breaks. Added "--ignore_conn_err"
#                    parameter to allow script to continue running in case of region connectivity issues.
#                    Without "--ignore_conn_err", script will crash during connection errors. Moved
#                    subset of test regions constant to aws_cleanup.py.
import sys
import os
import re
import random
import signal
try:
  import boto3
except ImportError as e:
  print('This script requires boto3 to be installed and configured.')
  print('Can install via "pip install boto3"')
  exit(1)
import argparse
import io
import textwrap
from botocore.exceptions import ClientError,NoCredentialsError,EndpointConnectionError
from collections import deque,defaultdict,namedtuple    # used for initializing nested dictionaries
try:
  from aws_cleanup_import import constantKeepTag, regionTestSubset, componentDef, awsComponentClass, aws_cleanup_import_ver
except ImportError:
  print('ERROR: aws_cleanup_import.py is missing. This file is required')
  exit(1)
def signal_handler(sig, frame):
        print('\nTERMINATING SCRIPT')
        sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

aws_cleanup_main_ver = 2.10
if aws_cleanup_import_ver != aws_cleanup_main_ver:
  print('WARNING: incorrect version of aws_cleanup_import.py file (version number is {0}; expected {1}).'.format(aws_cleanup_import_ver, aws_cleanup_main_ver))
  ign = input('Press enter to continue: ')

#  Setting up a named tuple for consolidating all the arguments passed plus a location
#  to store the normalized keepTag. Believe that Python 3.7 has a better
#  method for defining the "default".
scriptArgsTuple = namedtuple('scriptArgsTuple', ['inv', 'vpc_rebuild', 'del_all', 'ignore_conn_err', 'keepTag'])
scriptArgsTuple.__new__.__defaults__ = (False, False, False, False, None, False, constantKeepTag)

def formatDispName(*parNames):
  parNamesDisp = []
  for n in parNames:
    if n:
      parNamesDisp.append(n)
  if parNamesDisp:
    retName = ' ({})'.format(', '.join(parNamesDisp))
  else:
    retName = ''
  return retName

def dispYesNo(parBoolean):
  if parBoolean:
    return "Yes"
  else:
    return "No"

def chkRouteTablesAssociations(parRouteId, parScriptArg, parRegion):
  #  Digging through the route table associations to see if the route table is set as 'Main' 
  #  was repeated in a couple areas - easier to have as a function and include any
  #  associated subnets.
  clientEC2Route = boto3.client('ec2', region_name=parRegion)
  RouteTableIsMain = False
  RouteTableSubnets = []

  for idChk in clientEC2Route.describe_route_tables(Filters=[{'Name': 'route-table-id', 'Values':[parRouteId]}])['RouteTables']:
    if idChk['Associations']:
      for chkAssociations in idChk['Associations']:
        if chkAssociations.get('Main'):
          RouteTableIsMain = True
        if chkAssociations.get('SubnetId'):
          subnetInfo = clientEC2Route.describe_subnets(Filters=[{'Name':'subnet-id', 'Values':[chkAssociations['SubnetId']]}])['Subnets'][0]
          RouteTableSubnets.append(chkAssociations['SubnetId'] + tagNameFind(subnetInfo.get('Tags'), parScriptArg))
  return{'Main': RouteTableIsMain, 'Subnets': RouteTableSubnets}

class dispItemsLineClass:
  #  dispItemsLineClass - displays on a single line multiple item names
  #    that are deleted, detached, or revoked from a single AWS item. This will
  #    consolidate the output & provide info for possible debugging. For example,
  #    would be used when removing groups from a single user - the 
  #    output would look like
  #      User "scott" - removing group(s): ops, sec, timecard, travel
  #    Kept repeating this code; decided just to create a class for it.
  def __init__(self, parMsgPrefix):
    self.msg = parMsgPrefix
    self.itemSeparator = ', '

  def newItemName(self, parItemName):
    retBld = self.msg + parItemName
    self.msg = self.itemSeparator
    return retBld

  def EOL(self):
    if self.msg == self.itemSeparator:
      retVal = '\n'
    else:
      retVal = ''
    self.msg = None
    return retVal

class awsRpt:
  def __init__(self, par_title, *header):
    self.outputRpt = ""
    self.headerList = list(header)
    self.lines = "+"
    self.header = "|"
    self.title = par_title
    self.rows=0
    #  self.regionBreak variables - used for tracking when the region changes.
    self.regionBreak_newRpt = True
    self.reportBreak_colValueTrack = None

    #  Remove column headers with the value of "None".
    while None in self.headerList:
      self.headerList.remove(None)
    for col in self.headerList:
      if len(col) == 1:
        col.append(len(col[0]))
      if not col[1]:
        #  Default the column width to the size of the column heading
        col[1] = len(col[0])
      if len(col) == 2:
        #  Assume left justification
        col.append("<")
      if type(col[0]) != type(str()) or type(col[1]) != type(int()):
        print(col[0], col[1])
        print('ERROR - Invalid column header list parameter for awsRpt')
        print('FORMAT: awsRpt([["Title1",#],["Title2",#],["Title3,#"],...,["TitleN,#"]])')
        print(' WHERE: TitleN is the column header, and # is the width of the column')
        raise ValueError('awsRpt', 'Invalid column header list')
      col[1] = max(len(col[0]), col[1])
      self.header += '{0:^{fill}}'.format(col[0],fill=col[1]) + "|"
      self.lines += '-' * col[1] + "+"

  def passit(self):
    pass

  def addLine(self, rptBreakEnable, *rptRow):
    rptRowList = list(rptRow)

    #  Check to see if this report has breaks in it.
    if (type(rptBreakEnable) not in (type(bool()), type(int()))) or (type(rptBreakEnable) == type(int()) and rptBreakEnable < 1):
      raise ValueError('awsRpt.addLine', 'The first argument is boolean or int: True=report break using first col, int=report break using "int" col (starting with 1), False=disable report break')
    
    rptBreak = False
    if rptBreakEnable:
      if type(rptBreakEnable) == type(int()):
        rptBreakCol = rptBreakEnable - 1
      else:
        rptBreakCol = 0
      if self.reportBreak_colValueTrack is None:
        self.reportBreak_colValueTrack = rptRowList[rptBreakCol]
      else:
        if self.reportBreak_colValueTrack != rptRowList[rptBreakCol]:
          rptBreak = True
          self.reportBreak_colValueTrack = rptRowList[rptBreakCol]

    #  Do a quick check to make sure that the number of row columns matches the number of
    #  header columns. Need to loop through as some column values can be None, which
    #  is a skipped column.
    while None in rptRowList:
      rptRowList.remove(None)
    if (len(rptRowList) != len(self.headerList)):
       print ("ERROR - invalid column list for awsRpt.addLine()")
       print ("        The number of elements in the awsRpt.addLine column list (" + str(len(rptRowList))  + ") has to match")
       print ("        number of elements defined in column header (" + str(len(self.headerList)) + ")")
       raise ValueError('awsRpt.addLine', 'Incorrect number of elements in list parameter - has ' + str(len(rptRowList)) + " elements instead of " + str(len(self.headerList)))
    formColumn = []
    for colNo, colData in enumerate(rptRowList):
      formColumn.append(deque(textwrap.wrap(colData, width=self.headerList[colNo][1], subsequent_indent='   ')))
    anyData = True
    bldLine = "|"
    if rptBreak:
      self.outputRpt += "\n" + self.lines
      self.rows += 1
    while anyData:
      anyData = False
      bldLine = "|"
      for colNo, colData in enumerate(formColumn):
        try:
          chk=colData.popleft()
          anyData = True
          bldLine += '{0:{just}{fill}}'.format(chk,just=self.headerList[colNo][2],fill=self.headerList[colNo][1]) + "|"
        except:
          bldLine += " " * self.headerList[colNo][1] + "|"
          pass
      if anyData:
        if self.outputRpt:
          self.outputRpt += "\n" + bldLine
          self.rows += 1
        else:
          self.outputRpt = bldLine
          self.rows += 1

  def result(self):
    return self.lines + "\n" +  self.header + "\n" + self.lines + "\n" + self.outputRpt + "\n" + self.lines

  def resultf(self):
    if self.rows > 0:
      return "\n{0}\n{1}\n{2}\n{3}\n{4}\n{5}\n\n".format(self.title, self.lines, self.header, self.lines, self.outputRpt, self.lines)
    else:
      return ""

def tupleVal(parChkVal):
  #  As itemsKeep is processed as a tuple, added tupleVal function to reduce operating
  #  instructions and confusion, where ('abc') is a string and ('abc',) is a tuple.
  #  tupleVal converts string or tuple to a list, removing any blank values.
  #  Added error handling for non-string values
  retVal = []
  if type(parChkVal) == type(str()):
    if parChkVal:
      retVal=[parChkVal]
  elif type(parChkVal) == type(tuple()):
    #  Remove any blank values
    for chkContent in (parChkVal):
      if type(parChkVal) == type(str()):
        if chkContent:
          retVal.append(chkContent)
      else:
        raise SyntaxError("Invalid value")
  elif parChkVal is None:
    retVal = []
  else:
    raise SyntaxError("Invalid value")
  return retVal

def reScanItemsKeep(par_searchVal, par_componentDef):
  retItemKeep = ""
  for itemsKeep in tupleVal(par_componentDef.itemsKeep):
    if re.search('^'+re.escape(itemsKeep)+'$', par_searchVal, re.IGNORECASE):
      retItemKeep = "Yes"
  return retItemKeep

def tagNameFind(parTagList, parScriptArg):
  if parTagList is None:
    parTagList = []
  nameTagValue = dispKeepTagKeyList = ""
  keepTagKeyList = []
  for t in parTagList:
    if t['Key'] == 'Name':
      nameTagValue = t.get('Value')
    else:
      for searchKeepTag in parScriptArg.keepTag:
        if re.search('^'+re.escape(searchKeepTag)+'$', t.get('Key'), re.IGNORECASE):
          keepTagKeyList.append(t.get('Key'))
  if keepTagKeyList:
    dispKeepTagKeyList = " [{0}]".format(', '.join(keepTagKeyList))
  if nameTagValue or dispKeepTagKeyList:
    nameTag = " ({0}{1})".format(nameTagValue, dispKeepTagKeyList)
  else:
    nameTag = "" 
  return nameTag
  
class tagScan:
  #  tagScan contains AWS item object's derived attributes (at least as much as could be
  #  derived).
  def __init__(self, parTagList, parScriptArg):
    #  If parTagList is none, set as empty list to bypass for-loop.
    if parTagList is None:
      parTagList = []
    self.nameTag = ""
    self.delThisItem = False
    self.keepTagFound = ""
    for t in parTagList:
      if t['Key'] == 'Name':
        self.nameTag = t['Value']
      else:
        if parScriptArg.keepTag is not None:
          for searchKeepTag in parScriptArg.keepTag:
            if re.search('^'+re.escape(searchKeepTag)+'$', t['Key'], re.IGNORECASE):
              self.keepTagFound = "Yes"
    if not self.keepTagFound:
      if parScriptArg.del_all:
        self.delThisItem = True

argUsage = "usage: aws_cleanup.py -[h][--del][--vpc_rebuild][--ignore_conn_err]"
parser = argparse.ArgumentParser(allow_abbrev=False,usage=argUsage)
#  As "del" is a reserved word in Python, needed to have an alnternate destination.
parser.add_argument('-d', '--del', dest='delete', help='delete/terminate AWS components', action="store_true", default=False)
parser.add_argument('--vpc_rebuild', help='rebuild VPC default environment for all regions', action="store_true", default=False)
parser.add_argument('--region_test', help='reduces number of in-scope regions for code testing for better performance -ww', action="store_true", default=False)
parser.add_argument('--ignore_conn_err', help='during inventory, script will ignore connectivity errors to AWS regions', action="store_true", default=False)
args = parser.parse_args()
if args.delete:
  aws_cleanupArg = scriptArgsTuple(del_all=True, vpc_rebuild=args.vpc_rebuild, ignore_conn_err=args.ignore_conn_err)
else:
  aws_cleanupArg = scriptArgsTuple(inv=True, vpc_rebuild=args.vpc_rebuild, ignore_conn_err=args.ignore_conn_err)

keepTagHeader = [', '.join(aws_cleanupArg.keepTag)+"(Tag)","","^"]

awsComponent = awsComponentClass()
# Initialize the dictionary of items to delete/terminate
termTrack = defaultdict(lambda : defaultdict(dict))
noDeleteList = []
print('AWS components in-scope for {}:'.format(sys.argv[0]))
for id, idDetail in vars(awsComponent).items():
  if type(idDetail) is componentDef:
    print('  * {0} {1}'.format(idDetail.compName, ('' if idDetail.compDelete or aws_cleanupArg.inv else '\t*** DELETE DISABLED ***')))
    try:
      chkItemsKeep = tupleVal(idDetail.itemsKeep)
    except:
      print("ERROR in aws_cleanup_import.py for self.{0}: itemsKeep is not defined correctly.".format(id))
      print("\tCorrect format: itemKeep=('str1','str2','str3',...)")
      print("\t   Value found: itemKeep=",idDetail.itemsKeep,sep="")
      exit(12)
    if type(idDetail.compDelete) is not bool:
      print("ERROR in aws_cleanup_import.py for self.{0}: compDelete has an incorrect value.".format(id))
      print("\tCorrect formats: compDelete=True")
      print("\t                 compDelete=False")
      print("\t    Value found: compDelete=",idDetail.compDelete,sep="")
      exit(12)
    if idDetail.itemsKeep:
      print('\titemsKeep list for {0}: "{1}"'.format(idDetail.compName, '", "'.join(chkItemsKeep)))

    if not (aws_cleanupArg.inv or idDetail.compDelete):
      noDeleteList.append(idDetail.compName)
if aws_cleanupArg.keepTag:
  print('Tag used to identify which AWS items can\'t be terminated or deleted: {0}'.format(', '.join(aws_cleanupArg.keepTag)))
print("\n")
# Load all regions from AWS into region list.
# As this is where the initial connection occurs to AWS, included a couple traps to handle
# connectivity errors - network MIA, invalid AWS credentials, missing AWS credentials,....
try:
  regions = [region['RegionName'] for region in boto3.client('ec2').describe_regions()['Regions']]
except NoCredentialsError as e:
  print('ERROR: Cannot connect to AWS - possible credential issue.\nVerify that local AWS credentials in .aws are configured correctly.')
  exit(10)
except EndpointConnectionError as e:
  print('ERROR: Cannot connect to AWS - possible network issue.\nAWS error message: ', e)
  exit(11)
except:
  #  For any other errors...(there may be a real issue with obtaining AWS regions...).
  print("Unexpected error:", sys.exc_info()[0])
  #  If .aws directory doesn't exist, the error handling occurs here at the catch-all (strangely 
  #  enough, not handled in NoCredentialsError). Check to see if the .aws directory even 
  #  exists. If it isn't there, give a warning.
  if not os.path.isdir(os.path.expanduser('~/.aws')):
    print('It looks like the .aws directory for credentials is missing.')
  print('Make sure the local credentials are setup correctly - instructions can be found at https://aws.amazon.com/developers/getting-started/python')
  exit(12)
if args.region_test:
  regions=regionTestSubset  #for testing#
  print('Reduced regions for script testing: ', regions, '\n\n')

resourceIAM = boto3.resource('iam')
clientIAM = boto3.client('iam')
resourceS3 = boto3.resource('s3')
clientS3 = boto3.client('s3')
clientEC2 = boto3.client('ec2')

currentUserArn = resourceIAM.CurrentUser().arn
currentAccountId = resourceIAM.CurrentUser().arn.split(':')[-2]
currentAlias = ""
for getAlias in clientIAM.list_account_aliases()['AccountAliases']:
  currentAlias = getAlias
print('AWS Account ID/Alias:\t{0}{1}'.format(currentAccountId, formatDispName(currentAlias)))
print('Connected User:\t\t{0}'.format(re.sub('^.+/', '', currentUserArn.split(':')[-1])))

print('Inventory of ALL AWS components\n')
output=""
securityGroupDepend=defaultdict(lambda : defaultdict(dict))

#  Initiate all awsRpt instances for regions here. Could be programmatically done when the output
#  is generated, but too prone to errors.
EC2Rpt = awsRpt("{0}:".format(awsComponent.EC2.compName), *[["Region", 16],["Instance ID", 25],["Name(Tag)", 30],keepTagHeader, ["Image ID", 30],["Status", 13]])
SecurityGroupsRpt = awsRpt("{0}:".format(awsComponent.SecurityGroups.compName), *[["Region", 16],["Group ID", 25],["Name(Tag)", 30],keepTagHeader,["Group Name", 30],["Description", 35]])
VolumesRpt = awsRpt("{0}:".format(awsComponent.Volumes.compName), *[["Region", 16],["Volume ID", 25],["Name(Tag)", 30],keepTagHeader,["Vol Type", 10],["State", 15]])
KeyPairsRpt = awsRpt("{0}:".format(awsComponent.KeyPairs.compName), *[["Region", 16],["KeyName", 30],["Keep"]])
MetricAlarmsRpt = awsRpt("{0}:".format(awsComponent.MetricAlarms.compName), *[["Region", 16],["Alarm Name", 37],["Alarm Description", 40], ['State', 17], ["Namespace", 25], ["Metric Name", 30],["Keep"]])
CloudWatchLogGroupsRpt = awsRpt("{0}:".format(awsComponent.CloudWatchLogGroups.compName), *[["Region", 16],["Cloud Watch Log Group Name", 37],["Keep"]])
ConfigRulesRpt = awsRpt("{0}:".format(awsComponent.ConfigRules.compName), *[["Region", 16],["Config Rule Name", 37],["Rule Description", 70], ['State', 17] ,["Keep"]])
ConfigurationRecordersRpt = awsRpt("{0}:".format(awsComponent.ConfigurationRecorders.compName), *[["Region", 16],["Config Recorder Name", 37],["Recording?"],["Keep"]])
CloudFormationStacksRpt = awsRpt("{0}:".format(awsComponent.CloudFormationStacks.compName), *[["Region", 16], ["Name", 37],["Stack Status", 25],["Keep"]])
CloudTrailRpt = awsRpt("{0}:".format(awsComponent.CloudTrail.compName), *[["Home Region", 16],["Name", 37],["All Regions?"],["S3BucketName", 30],["Keep"]])
AssessmentTargetsRpt = awsRpt("{0}:".format(awsComponent.AssessmentTargets.compName), *[["Region", 16],["Assessment Target Name", 37],["Keep"]])
SNSTopicsRpt = awsRpt("{0}:".format(awsComponent.SNSTopics.compName), *[["Region", 16],["SNS Topic", 37],["Keep"]])
VPCRpt = awsRpt("{0}{1}:".format(awsComponent.VPC.compName, formatDispName('' if aws_cleanupArg.vpc_rebuild else 'non-default VPC')), *[["Region", 16],["CIDR Block", 20],["VPC ID", 25],["VPC Default"],["Name(Tag)", 30],keepTagHeader,["State", 10]])
RouteTablesRpt = awsRpt("{0}{1}:".format(awsComponent.RouteTables.compName, formatDispName('' if aws_cleanupArg.vpc_rebuild else 'for non-default VPCs')), *[["Region", 16], ["Route Table ID", 28],["VPC ID", 35],["Main", 4],["Name(Tag)", 30],keepTagHeader])
SubnetsRpt = awsRpt("{0}{1}:".format(awsComponent.Subnets.compName, formatDispName('' if aws_cleanupArg.vpc_rebuild else 'for non-default VPCs')), *[["Region", 16], ["CIDR Block", 20],["Subnet ID", 28],["VPC ID", 35],["Name(Tag)", 30],keepTagHeader,["State", 10]])
InternetGatewaysRpt = awsRpt("{0}{1}:".format(awsComponent.InternetGateways.compName, formatDispName('' if aws_cleanupArg.vpc_rebuild else 'for non-default VPCs')), *[["Region", 16],["Internet Gateway ID", 28],["Attached VPC", 35],["VPC Status",10],["Name(Tag)", 30],keepTagHeader])
VPCEndpointsRpt = awsRpt("{0}:".format(awsComponent.VPCEndpoints.compName), *[["Region", 16], ['Endpoint ID', 25], ['Endpoint Type', 20],['VPC ID', 35], ['Service Name', 45],['Keep']])

#  Eh... don't know if both lists are needed, but for future use will include VPCDefaultByRegion.
VPCDefaultByRegion = []
VPCNoDefaultByRegion = []
for currentRegion in sorted(regions):
  print ('Inventorying region {}...'.format(currentRegion))
  clientEC2Region = boto3.client('ec2',region_name=currentRegion)
  clientEventsRegion = boto3.client('events',region_name=currentRegion)
  clientCloudwatchRegion = boto3.client('cloudwatch', region_name=currentRegion)
  clientCloudWatchLogRegion = boto3.client('logs', region_name=currentRegion)
  clientCloudTrailRegion = boto3.client('cloudtrail', region_name=currentRegion)
  clientConfigRegion = boto3.client('config', region_name=currentRegion)
  clientSNSRegion = boto3.client('sns', region_name=currentRegion)
  clientInspectorRegion = boto3.client('inspector', region_name=currentRegion)
  clientCloudFormationRegion = boto3.client('cloudformation', region_name=currentRegion)

  #################################################################
  #  EC2 Instances
  #################################################################
  #  ...CompSci truth tables from WWU...
  if aws_cleanupArg.inv or awsComponent.EC2.compDelete:
    try:
      for resp in clientEC2Region.describe_instances()['Reservations']:
        for inst in resp['Instances']:
          tagData = tagScan(inst.get('Tags'), aws_cleanupArg)
          rptCommonLine = (True, currentRegion, inst['InstanceId'],tagData.nameTag,tagData.keepTagFound,inst['ImageId'],inst['State']['Name'])
          if aws_cleanupArg.inv:
            EC2Rpt.addLine(*rptCommonLine)
          elif inst['State']['Name'] != 'terminated':
            if tagData.delThisItem:
              EC2Rpt.addLine(*rptCommonLine)
              termTrack[awsComponent.EC2][currentRegion][inst['InstanceId']] = {'DISPLAY_ID': inst['InstanceId'] + formatDispName(tagData.nameTag),'TERMINATED':False}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.EC2.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  SecurityGroups
  #################################################################
  if aws_cleanupArg.inv or awsComponent.SecurityGroups.compDelete:
    try:
      for SecurityGroups in clientEC2Region.describe_security_groups()['SecurityGroups']:
        # ... can't do anything with the default security group
        if SecurityGroups['GroupName'] != 'default':
          tagData = tagScan(SecurityGroups.get('Tags'), aws_cleanupArg)
          rptCommonLine = (True, currentRegion, SecurityGroups['GroupId'],tagData.nameTag,tagData.keepTagFound,SecurityGroups['GroupName'],SecurityGroups['Description'])
          if aws_cleanupArg.inv:
            SecurityGroupsRpt.addLine(*rptCommonLine)
          elif tagData.delThisItem:
            SecurityGroupsRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.SecurityGroups][currentRegion][SecurityGroups['GroupId']] = {'DISPLAY_ID': SecurityGroups['GroupId'] + formatDispName(tagData.nameTag, SecurityGroups['GroupName'], SecurityGroups['Description'])}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.SecurityGroups.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)
          

  #################################################################
  #  Volumes
  #################################################################
  if aws_cleanupArg.inv or awsComponent.Volumes.compDelete:
    try:
      for Volumes in clientEC2Region.describe_volumes()['Volumes']:
        tagData = tagScan(Volumes.get('Tags'), aws_cleanupArg)
        rptCommonLine = (True, currentRegion, Volumes['VolumeId'],tagData.nameTag,tagData.keepTagFound,Volumes['VolumeType'],Volumes['State'])
        if aws_cleanupArg.inv:
          VolumesRpt.addLine(*rptCommonLine)
        elif tagData.delThisItem:
          VolumesRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.Volumes][currentRegion][Volumes['VolumeId']] = {'DISPLAY_ID': Volumes['VolumeId'] + formatDispName(tagData.nameTag)}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.Volumes.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  KeyPairs
  #################################################################
  if aws_cleanupArg.inv or awsComponent.KeyPairs.compDelete:
    try:
      for KeyPairs in clientEC2Region.describe_key_pairs()['KeyPairs']:
        chkItemKeep = reScanItemsKeep(KeyPairs['KeyName'], awsComponent.KeyPairs)
        rptCommonLine = (True, currentRegion, KeyPairs['KeyName'], chkItemKeep)
        if aws_cleanupArg.inv:
          KeyPairsRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          KeyPairsRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.KeyPairs][currentRegion][KeyPairs['KeyName']] = None
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.KeyPairs.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  MetricAlarms - Cloudwatch
  #################################################################
  if aws_cleanupArg.inv or awsComponent.MetricAlarms.compDelete:
    try:
      for MetricAlarms in clientCloudwatchRegion.describe_alarms()['MetricAlarms']:
        chkItemKeep = reScanItemsKeep(MetricAlarms['AlarmName'], awsComponent.MetricAlarms)
        rptCommonLine = (True, currentRegion, MetricAlarms['AlarmName'], str(MetricAlarms.get('AlarmDescription') or ''), MetricAlarms.get('StateValue'), MetricAlarms.get('Namespace'), MetricAlarms.get('MetricName'),chkItemKeep)
        if aws_cleanupArg.inv:
          MetricAlarmsRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          MetricAlarmsRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.MetricAlarms][currentRegion][MetricAlarms['AlarmName']] = {'DISPLAY_ID': MetricAlarms['AlarmName'] + formatDispName(MetricAlarms.get('AlarmDescription'))}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.MetricAlarms.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  CloudWatchLogGroups
  #################################################################
  if aws_cleanupArg.inv or awsComponent.CloudWatchLogGroups.compDelete:
    try:
      for CloudWatchLogGroups in clientCloudWatchLogRegion.describe_log_groups()['logGroups']:
        chkItemKeep = reScanItemsKeep(CloudWatchLogGroups['logGroupName'], awsComponent.CloudWatchLogGroups)
        rptCommonLine = (True, currentRegion, CloudWatchLogGroups['logGroupName'],chkItemKeep)
        if aws_cleanupArg.inv:
          CloudWatchLogGroupsRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          CloudWatchLogGroupsRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.CloudWatchLogGroups][currentRegion][CloudWatchLogGroups['logGroupName']] = None
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.CloudWatchLogGroups.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)


  #################################################################
  #  ConfigRules
  #################################################################
  if aws_cleanupArg.inv or awsComponent.ConfigRules.compDelete:
    try:
      for ConfigRules in clientConfigRegion.describe_config_rules()['ConfigRules']:
        chkItemKeep = reScanItemsKeep(ConfigRules['ConfigRuleName'], awsComponent.ConfigRules)
        rptCommonLine = (True, currentRegion, ConfigRules['ConfigRuleName'], str(ConfigRules.get('Description') or ''), ConfigRules.get('ConfigRuleState'), chkItemKeep)
        if aws_cleanupArg.inv:
          ConfigRulesRpt.addLine(*rptCommonLine)
        elif not chkItemKeep and ConfigRules.get('ConfigRuleState') != "DELETING":
          ConfigRulesRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.ConfigRules][currentRegion][ConfigRules['ConfigRuleName']] = {'DISPLAY_ID': ConfigRules['ConfigRuleName'] + formatDispName(ConfigRules.get('Description'))}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.ConfigRules.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  ConfigurationRecorders
  #################################################################
  if aws_cleanupArg.inv or awsComponent.ConfigurationRecorders.compDelete:
    try:
      for ConfigurationRecorders in clientConfigRegion.describe_configuration_recorder_status()['ConfigurationRecordersStatus']:
        chkItemKeep = reScanItemsKeep(ConfigurationRecorders['name'], awsComponent.ConfigurationRecorders)
        rptCommonLine = (True, currentRegion, ConfigurationRecorders['name'], dispYesNo(ConfigurationRecorders.get('recording')), chkItemKeep)
        if aws_cleanupArg.inv:
          ConfigurationRecordersRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          ConfigurationRecordersRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.ConfigurationRecorders][currentRegion][ConfigurationRecorders['name']] = None
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.ConfigurationRecorders.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  CloudFormationStacks
  #################################################################
  if aws_cleanupArg.inv or awsComponent.CloudFormationStacks.compDelete:
    try:
      for CloudFormationStacks in clientCloudFormationRegion.list_stacks()['StackSummaries']:
        chkItemKeep = reScanItemsKeep(CloudFormationStacks['StackName'], awsComponent.CloudFormationStacks)
        rptCommonLine = (True, currentRegion, CloudFormationStacks['StackName'],CloudFormationStacks['StackStatus'], chkItemKeep)
        if aws_cleanupArg.inv:
          CloudFormationStacksRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          #  Included the most likely status below...
          if CloudFormationStacks['StackStatus'] not in ('DELETE_IN_PROGRESS', 'DELETE_FAILED', 'DELETE_COMPLETE'):
            CloudFormationStacksRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.CloudFormationStacks][currentRegion][CloudFormationStacks['StackId']] = {'DISPLAY_ID': CloudFormationStacks['StackName']}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.CloudFormationStacks.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  CloudTrail
  #################################################################
  if aws_cleanupArg.inv or awsComponent.CloudTrail.compDelete:
    try:
      for CloudTrail in clientCloudTrailRegion.describe_trails()['trailList']:
        if not (CloudTrail['IsMultiRegionTrail'] and CloudTrail['HomeRegion'] != currentRegion):
          chkItemKeep = reScanItemsKeep(CloudTrail['Name'], awsComponent.CloudTrail)
          rptCommonLine = (True, currentRegion, CloudTrail['Name'], 'Yes' if CloudTrail['IsMultiRegionTrail'] else "", CloudTrail['S3BucketName'], chkItemKeep)
          if aws_cleanupArg.inv:
            CloudTrailRpt.addLine(*rptCommonLine)
          elif not chkItemKeep:
            CloudTrailRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.CloudTrail][currentRegion][CloudTrail['TrailARN']] = {'DISPLAY_ID': CloudTrail['Name']}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.CloudTrail.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  AssessmentTargets 
  #################################################################
  if aws_cleanupArg.inv or awsComponent.AssessmentTargets.compDelete:
    #  If a region desn't have Inspector, list_assessment_targets will raise an EndpointConnectionError.
    #  The "try:" below will catch that error and keep the script executing. May need this for
    #  other cases.
    try:
      for AssessmentTargetsArn in clientInspectorRegion.list_assessment_targets()['assessmentTargetArns']:
        for AssessmentTargets in clientInspectorRegion.describe_assessment_targets(assessmentTargetArns = [AssessmentTargetsArn])['assessmentTargets']:
          chkItemKeep = reScanItemsKeep(AssessmentTargets['name'], awsComponent.AssessmentTargets)
          rptCommonLine = (True, currentRegion, AssessmentTargets['name'], chkItemKeep)
          if aws_cleanupArg.inv:
            AssessmentTargetsRpt.addLine(*rptCommonLine)
          elif not chkItemKeep:
            AssessmentTargetsRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.AssessmentTargets][currentRegion][AssessmentTargetsArn] = {'DISPLAY_ID': AssessmentTargets['name']}
    except EndpointConnectionError: 
      pass
   

  #################################################################
  #  SNSTopics
  #################################################################
  if aws_cleanupArg.inv or awsComponent.SNSTopics.compDelete:
    try:
      for SNSTopics in clientSNSRegion.list_topics()['Topics']:
        chkItemKeep = reScanItemsKeep(SNSTopics['TopicArn'].split(':')[-1], awsComponent.SNSTopics)
        rptCommonLine = (True, currentRegion, SNSTopics['TopicArn'].split(':')[-1], chkItemKeep)
        if aws_cleanupArg.inv:
          SNSTopicsRpt.addLine(*rptCommonLine)
        elif not chkItemKeep:
          SNSTopicsRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.SNSTopics][currentRegion][SNSTopics['TopicArn']] = {'DISPLAY_ID': SNSTopics['TopicArn'].split(':')[-1]}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.SNSTopics.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)
          

  #################################################################
  #  VPC
  #################################################################
  if aws_cleanupArg.inv or awsComponent.VPC.compDelete:
    try:
      VPCThereIsDefault = False
      for VPC in clientEC2Region.describe_vpcs()['Vpcs']:
        if VPC['IsDefault']:
          VPCThereIsDefault = True
        if aws_cleanupArg.vpc_rebuild or (not aws_cleanupArg.vpc_rebuild and not VPC['IsDefault']):
          tagData = tagScan(VPC.get('Tags'), aws_cleanupArg)
          rptCommonLine = (True, currentRegion, VPC['CidrBlock'], VPC['VpcId'], dispYesNo(VPC['IsDefault']), tagData.nameTag,tagData.keepTagFound,VPC['State'])
          if aws_cleanupArg.inv:
            VPCRpt.addLine(*rptCommonLine)
          elif tagData.delThisItem:
            VPCRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.VPC][currentRegion][VPC['VpcId']] = {'DISPLAY_ID': VPC['VpcId'] + formatDispName(tagData.nameTag, VPC['CidrBlock'])}
      if VPCThereIsDefault:
        VPCDefaultByRegion.append(currentRegion)
      else:
        VPCNoDefaultByRegion.append(currentRegion)
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.VPC.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  RouteTables
  #################################################################
  if aws_cleanupArg.inv or awsComponent.RouteTables.compDelete:
    try:
      for RouteTables in clientEC2Region.describe_route_tables()['RouteTables']:
        tagData = tagScan(RouteTables.get('Tags'), aws_cleanupArg)
        RouteTablesAssociations = chkRouteTablesAssociations(RouteTables['RouteTableId'], aws_cleanupArg, currentRegion)
        if RouteTablesAssociations['Main']:
          RouteTablesDispMain = "Yes"
        else:
          RouteTablesDispMain = "No"
        isVPCDefault = False
        for chkVpc in clientEC2Region.describe_vpcs(VpcIds=[RouteTables['VpcId']], Filters=[{'Name': 'isDefault', 'Values':['true']}])['Vpcs']:
          isVPCDefault = True
        if aws_cleanupArg.vpc_rebuild or not isVPCDefault:
          rptCommonLine = (True, currentRegion, RouteTables['RouteTableId'], RouteTables['VpcId'] + (" (default)" if isVPCDefault else ""), RouteTablesDispMain, tagData.nameTag,tagData.keepTagFound)
          if aws_cleanupArg.inv:
            RouteTablesRpt.addLine(*rptCommonLine)
          elif tagData.delThisItem:
            RouteTablesRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.RouteTables][currentRegion][RouteTables['RouteTableId']] = {'DISPLAY_ID': RouteTables['RouteTableId'] + formatDispName(tagData.nameTag),'VpcId':RouteTables['VpcId']}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.RouteTables.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  Subnets
  #################################################################
  if aws_cleanupArg.inv or awsComponent.Subnets.compDelete:
    try:
      for Subnets in clientEC2Region.describe_subnets()['Subnets']:
        isVPCDefault = False
        for chkVpc in clientEC2Region.describe_vpcs(VpcIds=[Subnets['VpcId']], Filters=[{'Name': 'isDefault', 'Values':['true']}])['Vpcs']:
          isVPCDefault = True
        if aws_cleanupArg.vpc_rebuild or not isVPCDefault:
          tagData = tagScan(Subnets.get('Tags'), aws_cleanupArg)
          rptCommonLine = (True, currentRegion, Subnets['CidrBlock'], Subnets['SubnetId'], Subnets['VpcId']  + (" (default)" if isVPCDefault else ""), tagData.nameTag,tagData.keepTagFound,Subnets['State'])
          if aws_cleanupArg.inv:
            SubnetsRpt.addLine(*rptCommonLine)
          elif tagData.delThisItem:
            SubnetsRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.Subnets][currentRegion][Subnets['SubnetId']] = {'DISPLAY_ID': Subnets['SubnetId'] + formatDispName(tagData.nameTag, Subnets['CidrBlock']), 'VpcId': Subnets['VpcId']}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.Subnets.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  InternetGateways
  #################################################################
  if aws_cleanupArg.inv or awsComponent.InternetGateways.compDelete:
    try:
      for InternetGateways  in clientEC2Region.describe_internet_gateways()['InternetGateways']:
        if InternetGateways['Attachments']:
          InternetGatewaysDispVpcId = InternetGateways['Attachments'][0]['VpcId']
          InternetGatewaysDispState = InternetGateways['Attachments'][0]['State']
        else:
          InternetGatewaysDispVpcId = ''
          InternetGatewaysDispState = ''
        isVPCDefault = False
        for chkVpc in clientEC2Region.describe_vpcs(VpcIds=[InternetGatewaysDispVpcId], Filters=[{'Name': 'isDefault', 'Values':['true']}])['Vpcs']:
          isVPCDefault = True
        if aws_cleanupArg.vpc_rebuild or not isVPCDefault:
          tagData = tagScan(InternetGateways.get('Tags'), aws_cleanupArg)
          rptCommonLine = (True, currentRegion, InternetGateways['InternetGatewayId'], InternetGatewaysDispVpcId + (" (default)" if isVPCDefault else ""), InternetGatewaysDispState, tagData.nameTag,tagData.keepTagFound)
          if aws_cleanupArg.inv:
            InternetGatewaysRpt.addLine(*rptCommonLine)
          elif tagData.delThisItem:
            InternetGatewaysRpt.addLine(*rptCommonLine)
            termTrack[awsComponent.InternetGateways][currentRegion][InternetGateways['InternetGatewayId']] = {'DISPLAY_ID': InternetGateways['InternetGatewayId'] + formatDispName(tagData.nameTag),'VpcID':InternetGatewaysDispVpcId}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.InternetGateways.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

  #################################################################
  #  VPCEndpoints
  #################################################################
  if aws_cleanupArg.inv or awsComponent.VPCEndpoints.compDelete:
    try:
      for VPCEndpoints in clientEC2Region.describe_vpc_endpoints()['VpcEndpoints']:
        isVPCDefault = False
        for chkVpc in clientEC2Region.describe_vpcs(VpcIds=[VPCEndpoints['VpcId']], Filters=[{'Name': 'isDefault', 'Values':['true']}])['Vpcs']:
          isVPCDefault = True
        chkItemKeep = reScanItemsKeep(VPCEndpoints['VpcEndpointId'], awsComponent.VPCEndpoints)
        rptCommonLine = (True, currentRegion, VPCEndpoints['VpcEndpointId'], VPCEndpoints['VpcEndpointType'], VPCEndpoints['VpcId']  + (" (default)" if isVPCDefault else ""),VPCEndpoints['ServiceName'], chkItemKeep)

        if aws_cleanupArg.inv:
          VPCEndpointsRpt.addLine(*rptCommonLine)
        elif not chkItemKeep: 
          VPCEndpointsRpt.addLine(*rptCommonLine)
          termTrack[awsComponent.VPCEndpoints][currentRegion][VPCEndpoints['VpcEndpointId']] = {'DISPLAY_ID': VPCEndpoints['VpcEndpointId'] + formatDispName(VPCEndpoints['VpcEndpointType'],VPCEndpoints['ServiceName'])}
    except EndpointConnectionError:
      print('\tComponent "{0}" - cannot connect to region {1}'.format(awsComponent.VPCEndpoints.compName, currentRegion))
      if aws_cleanupArg.ignore_conn_err:
        pass
      else:
        exit(100)

output += EC2Rpt.resultf()
output += SecurityGroupsRpt.resultf()
output += VolumesRpt.resultf()
output += KeyPairsRpt.resultf() 
output += VPCRpt.resultf()
if VPCNoDefaultByRegion:
  output += '\nThe following regions do not have default VPCs: {0}'.format(', '.join(VPCNoDefaultByRegion)) + ("\n" * 2)
output += RouteTablesRpt.resultf()
output += SubnetsRpt.resultf()
output += InternetGatewaysRpt.resultf()
output += VPCEndpointsRpt.resultf() 
output += MetricAlarmsRpt.resultf() 
output += CloudWatchLogGroupsRpt.resultf()
output += ConfigRulesRpt.resultf()
output += ConfigurationRecordersRpt.resultf()
output += CloudFormationStacksRpt.resultf()
output += CloudTrailRpt.resultf()
output += AssessmentTargetsRpt.resultf()
output += SNSTopicsRpt.resultf()

#################################################################
#  S3
#################################################################
S3Rpt = awsRpt("{0}:".format(awsComponent.S3.compName), *[["Bucket Name", 40],keepTagHeader])
if aws_cleanupArg.inv or awsComponent.S3.compDelete:
  for buckets in clientS3.list_buckets()['Buckets']:
    try:
      bucketTag = clientS3.get_bucket_tagging(Bucket=buckets['Name'])['TagSet']
    except ClientError as e:
      bucketTag=[]
    tagData = tagScan(bucketTag, aws_cleanupArg)
    rptCommonLine = (False, buckets['Name'], tagData.keepTagFound)
    if aws_cleanupArg.inv:
      S3Rpt.addLine(*rptCommonLine)
    elif tagData.delThisItem:
      S3Rpt.addLine(*rptCommonLine)
      termTrack[awsComponent.S3][buckets['Name']] = None
output += S3Rpt.resultf()

#################################################################
#  Users 
#################################################################
currentUserArnDel = False
UsersRpt = awsRpt("{0}:".format(awsComponent.Users.compName),*[["User Name", 20], ["ARN", 50], ["Keep"]])
if aws_cleanupArg.inv or awsComponent.Users.compDelete:
  for Users in clientIAM.list_users()['Users']:
    chkItemKeep = reScanItemsKeep(Users['UserName'], awsComponent.Users)
    rptCommonLine = (False, Users['UserName'], Users['Arn'],chkItemKeep)
    if aws_cleanupArg.inv:
      UsersRpt.addLine(*rptCommonLine)
    elif not chkItemKeep:
      UsersRpt.addLine(*rptCommonLine)
      # Safety feature - don't let the current connected user be deleted.
      if currentUserArn == Users['Arn']:
        currentUserArnDel  = True
      else:
        termTrack[awsComponent.Users][Users['UserName']] = {'DISPLAY_ID': Users['Arn']}
output += UsersRpt.resultf()
    
#################################################################
#  Groups 
#################################################################
GroupsRpt = awsRpt("{0}:".format(awsComponent.Groups.compName),*[["Group Name", 60], ["Keep"]])
if aws_cleanupArg.inv or awsComponent.Groups.compDelete:
  for Groups in clientIAM.list_groups()['Groups']:
    chkItemKeep = reScanItemsKeep(Groups['GroupName'], awsComponent.Groups)
    rptCommonLine=(False, Groups['GroupName'],chkItemKeep)
    if aws_cleanupArg.inv:
      GroupsRpt.addLine(*rptCommonLine)
    elif not chkItemKeep:
      GroupsRpt.addLine(*rptCommonLine)
      termTrack[awsComponent.Groups][Groups['GroupName']] = None
output += GroupsRpt.resultf()

#################################################################
#  Policies 
#################################################################
PoliciesRpt = awsRpt("{0}:".format(awsComponent.Policies.compName),*[["Policy Name", 70], ["Description", 40], ["Keep"]])
if aws_cleanupArg.inv or awsComponent.Policies.compDelete:
  for Policies in clientIAM.list_policies(Scope='Local')['Policies']:
    chkItemKeep = reScanItemsKeep(Policies['PolicyName'], awsComponent.Policies)
    rptCommonLine=(False, Policies['PolicyName'], str(Policies.get('Description') or ''), chkItemKeep)
    if aws_cleanupArg.inv:
      PoliciesRpt.addLine(*rptCommonLine)
    elif not chkItemKeep:
      PoliciesRpt.addLine(*rptCommonLine)
      termTrack[awsComponent.Policies][Policies['Arn']] = {'DISPLAY_ID': Policies['PolicyName']}
output += PoliciesRpt.resultf()

#################################################################
#  Roles
#################################################################
RolesRpt = awsRpt("{0}:".format(awsComponent.Roles.compName),*[["Role Name", 75], ["AWS Service"], ["Keep"]])
if aws_cleanupArg.inv or awsComponent.Roles.compDelete:
  for Roles in clientIAM.list_roles()['Roles']:
    chkItemKeep = reScanItemsKeep(Roles['RoleName'], awsComponent.Roles)
    if re.search('^/aws-service-role/',Roles['Path']):
      Roles_IsAwsService = True
    else:
      Roles_IsAwsService = False
    if aws_cleanupArg.inv:
      RolesRpt.addLine(False, Roles['RoleName'],dispYesNo(Roles_IsAwsService), chkItemKeep)
    elif not chkItemKeep:
      if Roles['RoleName'] == 'AWSServiceRoleForSupport':
        #  Special handing for role AWSServiceRoleForSupport - this cannot be deleted.
        RolesRpt.addLine(False, '{0} - this service-linked role cannot be deleted. Review AWS support docs for details'.format(Roles['RoleName']),dispYesNo(Roles_IsAwsService), "Yes")
      elif Roles['RoleName'] == 'AWSServiceRoleForTrustedAdvisor':
        #  Special handling for role AWSServiceRoleForTrustedAdvisor 
        RolesRpt.addLine(False, '{0} - service-linked role isn\'t removed by this script. To manually remove, seach AWS documentation for "Deleting a Service-Linked Role for Trusted Advisor"'.format(Roles['RoleName']),dispYesNo(Roles_IsAwsService), "Yes")
      else:
        RolesRpt.addLine(False, Roles['RoleName'],dispYesNo(Roles_IsAwsService), chkItemKeep)
        termTrack[awsComponent.Roles][Roles['RoleName']] = {'IsAwsService': Roles_IsAwsService}
output += RolesRpt.resultf()

#################################################################
#  InstanceProfiles
#################################################################
InstanceProfilesRpt = awsRpt("{0}:".format(awsComponent.InstanceProfiles.compName), *[["Instance Profile Name", 75], ["Keep"]])
if aws_cleanupArg.inv or awsComponent.InstanceProfiles.compDelete:
  for InstanceProfiles in clientIAM.list_instance_profiles()['InstanceProfiles']:
    chkItemKeep = reScanItemsKeep(InstanceProfiles['InstanceProfileName'], awsComponent.InstanceProfiles)
    rptCommonLine = (False, InstanceProfiles['InstanceProfileName'],chkItemKeep)
    if aws_cleanupArg.inv:
      InstanceProfilesRpt.addLine(*rptCommonLine)
    elif not chkItemKeep:
      InstanceProfilesRpt.addLine(*rptCommonLine)
      termTrack[awsComponent.InstanceProfiles] = [InstanceProfiles['InstanceProfileName']] = None
output += InstanceProfilesRpt.resultf()

print(output)
print("\n")
if not aws_cleanupArg.inv:

  if currentUserArnDel:
    currentUserArnDelMsg = '\n' + '*' * 100 + '\n'
    currentUserArnDelMsg += 'ERROR: Your connected username "{0}" (from  ~/.aws/credentials) is targeted for deletion.'.format(re.sub('^.+/', '', currentUserArn.split(':')[-1])) + '\n'
    currentUserArnDelMsg += '\taws_cleanup.py will bypass deleting account {0}, but no guarantees on groups and/or\n\tpolicies granted to {0} being deleted & {0} loosing authorization.\n\n\tYou can configure "{1}" to be excluded from deletion\n\tin aws_cleanup_import.py, or re-configure ~/.aws/credentials for the root account.'.format(re.sub('^.+/', '', currentUserArn.split(':')[-1]), currentUserArn) + "\n"
    currentUserArnDelMsg += '*' * 100 + "\n"


  if not termTrack and (not aws_cleanupArg.vpc_rebuild or (aws_cleanupArg.vpc_rebuild and not VPCNoDefaultByRegion)):
    if currentUserArnDel:
      print(currentUserArnDelMsg)
    print("No AWS items found that are in-scope for terminating/deleting")
  else:
    #  Verify that they really want to terminate/delete everything listed as in-scope.
    if VPCNoDefaultByRegion and not termTrack:
      print("No AWS items found that are in-scope for terminating/deleting; however")
      print("the following regions don't have default VPCs: {0}".format(', '.join(VPCNoDefaultByRegion)))
      print("Default VPCs are created in all the regions when your AWS environment is setup.")
      verifyDelCode = str(random.randint(0, 9999)).zfill(4)
      print("\nVerification Code ---> {}".format(verifyDelCode))
      verifyTermProceed = input('Enter above 4-digit Verification Code to re-create missing default VPCs (ctrl-c to exit): ')
    else:
      if aws_cleanupArg.del_all:
        print("Terminating/deleting ALL components")
      if noDeleteList:
        print('REMEMBER - DELETION/TERMINATION HAS BEEN DISABLED FOR THE FOLLOWING AWS COMPONENTS:\n\t{}'.format('\n\t'.join(noDeleteList)))
      if currentUserArn.split(':')[-1] != "root" and not currentUserArnDel:
        print("WARNING: while ~/.aws/credentials (username {0}) is out of scope for deletion,".format(re.sub('^.+/', '', currentUserArn.split(':')[-1])))
        print("         you are responsible for verifing the groups and policies for account {0}".format(re.sub('^.+/', '', currentUserArn.split(':')[-1])))
        print("         remain intact for future authorizations.")
      if currentUserArnDel:
        print(currentUserArnDelMsg)
        ign = input("Proceed at your own risk - press enter to continue: ")
      #  Having the user type in something more that just "yes" to confirm they really
      #  want to terminate/delete AWS item(s).
      verifyDelCode = str(random.randint(0, 9999)).zfill(4)
      print("\nALL AWS COMPONENTS LISTED ABOVE WILL BE TERMINATED/DELETED. Verification Code ---> {}".format(verifyDelCode))
      verifyTermProceed = input('Enter above 4-digit Verification Code to proceed (ctrl-c to exit): ')
    #################################################################
    #  EC2 Instances terminate
    #################################################################
    if verifyTermProceed == verifyDelCode:
      for currentRegion,idDict in termTrack.get(awsComponent.EC2, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)

        for id, idDetail in idDict.items():
          print('Terminating ' + currentRegion + ' EC2 instance ' + idDetail['DISPLAY_ID'])
          ec2DryRunSuccessful=False
          #  Not sure if there's an advantage to having the DryRun test;
          #  will leave this segment of code in place for future.
          try:
            response = clientEC2Region.terminate_instances(InstanceIds=[id], DryRun=True)
            #  Should never reach this point, but just in case...
            ec2DryRunSuccess = True
          except ClientError as e:
            if e.response["Error"]["Code"] == "DryRunOperation":
              ec2DryRunSuccess = True
            else:
              print(e, '\n')
              ec2DryRunSuccess = False
          if ec2DryRunSuccess:
            try:
              response = clientEC2Region.terminate_instances(InstanceIds=[id], DryRun=False)
              idDetail['TERMINATED'] = True
            except ClientError as e:
              print("    ERROR:", e, '\n')
      #  Loop through terminated instances and wait for the termination to 
      #  complete before continuing.
      for currentRegion,idDict in termTrack.get(awsComponent.EC2, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        waiter = clientEC2Region.get_waiter('instance_terminated')
        for id, idDetail in idDict.items():
          if idDetail['TERMINATED']:
            print('Waiting for {0} EC2 instance {1} to terminate...'.format(currentRegion, idDetail['DISPLAY_ID']))
            waiter.wait(InstanceIds=[id])
             
      #################################################################
      #  SecurityGroups delete
      #################################################################
      #  Delete Security Groups
      for currentRegion,idDict in termTrack.get(awsComponent.SecurityGroups, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting ' + currentRegion + ' Security Group ' + idDetail['DISPLAY_ID'])
          conflictList = []
          for instResvChk in clientEC2Region.describe_instances(Filters=[{'Name': 'instance.group-id', 'Values': [id]}])['Reservations']:
            for instChk in instResvChk['Instances']:
              conflictList.append(instChk['InstanceId'] + tagNameFind(instChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: Security Group {0} is attached to the following EC2 instance(s):\n\t{1}'.format(id, '\n\t'.join(conflictList)))
          try:
            response = clientEC2Region.delete_security_group(GroupId = id, DryRun=False)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  Volumes delete
      #################################################################
      if awsComponent.Volumes in termTrack:
        print("NOTE: Volumes may already been deleted with assoicated EC2 instances.")
        for currentRegion,idDict in termTrack.get(awsComponent.Volumes, {}).items():
          clientEC2Region = boto3.client('ec2',region_name=currentRegion)
          for id, idDetail in idDict.items():
            print('Deleting ' + currentRegion + ' Volume ' + idDetail['DISPLAY_ID'])
            try:
              response = clientEC2Region.delete_volume(VolumeId = id, DryRun=False)
            except ClientError as e:
              if e.response["Error"]["Code"] == 'InvalidVolume.NotFound':
                print("    Volume already deleted")
              else:
                print("    ERROR:", e, '\n')


      #################################################################
      #  KeyPairs delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.KeyPairs, {}).items():
        clientEC2Region = boto3.resource('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} "{1}"'.format(awsComponent.KeyPairs.compName, id))
          try:
            response = clientEC2Region.KeyPair(id).delete(DryRun=False)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  MetricAlarms delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.MetricAlarms, {}).items():
        clientCloudwatchRegion = boto3.client('cloudwatch', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} alarm {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          try:
            ign = clientCloudwatchRegion.delete_alarms(AlarmNames=[id])
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  CloudWatchLogGroups delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.CloudWatchLogGroups, {}).items():
        clientCloudWatchLogRegion = boto3.client('logs', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} {1} "{2}"'.format(currentRegion, awsComponent.CloudWatchLogGroups.compName, id))
          try:
            ign = clientCloudWatchLogRegion.delete_log_group(logGroupName=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  ConfigRules delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.ConfigRules, {}).items():
        clientConfigRegion = boto3.client('config', region_name=currentRegion)
        for id, idDetail in idDict.items():
          #  The description for ConfigRules can get wordy; leaving off for the moment.
          print('Deleting {0} {1} "{2}"'.format(currentRegion, awsComponent.ConfigRules.compName, id))
          try:
            ign = clientConfigRegion.delete_config_rule(ConfigRuleName=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  CloudFormationStacks delete 
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.CloudFormationStacks, {}).items():
        clientCloudFormationRegion = boto3.client('cloudformation', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} {1} "{2}"'.format(currentRegion, awsComponent.CloudFormationStacks.compName, idDetail['DISPLAY_ID']))
          try:
            ign = clientCloudFormationRegion.delete_stack(StackName=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  CloudTrail delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.CloudTrail, {}).items():
        clientCloudTrailRegion = boto3.client('cloudtrail', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} {1} "{2}"'.format(currentRegion, awsComponent.CloudTrail.compName, idDetail['DISPLAY_ID']))
          try:
            ign = clientCloudTrailRegion.delete_trail(Name=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  ConfigurationRecorders delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.ConfigurationRecorders, {}).items():
        clientConfigRegion = boto3.client('config', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} {1} "{2}"'.format(currentRegion, awsComponent.ConfigurationRecorders.compName, id))
          try:
            response = clientConfigRegion.delete_configuration_recorder(ConfigurationRecorderName=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  AssessmentTargets delete 
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.AssessmentTargets, {}).items():
        clientInspectorRegion = boto3.client('inspector', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} Assessment Target {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          try:
            response = clientInspectorRegion.delete_assessment_target(assessmentTargetArn=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  SNSTopics delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.SNSTopics, {}).items():
        clientSNSRegion = boto3.client('sns', region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} SNS Topic {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          try:
            response = clientSNSRegion.delete_topic(TopicArn=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  VPCEndpoints delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.VPCEndpoints, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} VPC Endpoint {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          try:
            ign = clientEC2Region.delete_vpc_endpoints(VpcEndpointIds=[id])
          except ClientError as e:
            print("    ERROR:", e, '\n')


      #################################################################
      #  Subnets delete
      #################################################################
      for currentRegion,idDict in termTrack.get(awsComponent.Subnets, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} subnet {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          conflictList = []
          for instResvChk in clientEC2Region.describe_instances(Filters=[{'Name': 'subnet-id', 'Values': [id]}])['Reservations']:
            for instChk in instResvChk['Instances']:
              conflictList.append(instChk['InstanceId'] + tagNameFind(instChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: {0} subnet {1} is associated with the following EC2 instance(s):\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          conflictList = []
          for idChk in clientEC2Region.describe_vpc_endpoints(Filters=[{'Name': 'vpc-id', 'Values': [idDetail['VpcId']]}])['VpcEndpoints']:
            if id in idChk['SubnetIds']:
              conflictList.append(idChk['VpcEndpointId'])
          if conflictList:
            print('  WARNING: {0} subnet {1} is associated with the following endpoints:\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          try:
            ign = clientEC2Region.delete_subnet(SubnetId=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  RouteTables delete
      #################################################################
      for currentRegion, idDict in termTrack.get(awsComponent.RouteTables, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          print('Deleting {0} Route Table {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          delRouteTables = True
          #  Check main
          RouteTablesAssociations = chkRouteTablesAssociations(id, aws_cleanupArg, currentRegion)
          if RouteTablesAssociations['Main']:
            chkVpc = clientEC2Region.describe_vpcs(VpcIds=[idDetail['VpcId']])['Vpcs'][0]
            if awsComponent.VPC in termTrack and currentRegion in termTrack[awsComponent.VPC] and idDetail['VpcId'] in termTrack[awsComponent.VPC][currentRegion]:
              print('  NOTE: {0} {1} is the Main route table for VPC {2}; it\'s deleted automatically when the VPC is deleted.\n'.format(currentRegion, id, idDetail['VpcId'] + tagNameFind(chkVpc.get('Tags'), aws_cleanupArg)))
              delRouteTables = False
            else:
              print('  WARNING: {0} {1} is the Main route table for VPC {2}; it cannot be deleted until the VPC is in-scope for deletion.'.format(currentRegion, id, idDetail['VpcId']  + tagNameFind(chkVpc.get('Tags'), aws_cleanupArg)))
          if delRouteTables:
            if RouteTablesAssociations['Subnets'] and not RouteTablesAssociations['Main']:
              print('  WARNING: {0} Route Table {1} is associated with the following subnets:\n\t{2}.'.format(currentRegion, id, '\n\t'.join(RouteTablesAssociations['Subnets'])))
            try:
              ign = clientEC2Region.delete_route_table(RouteTableId=id)
            except ClientError as e:
              print("    ERROR:", e, '\n')

      #################################################################
      #  InternetGateways delete
      #################################################################
      for currentRegion, idDict in termTrack.get(awsComponent.InternetGateways, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():
          error_detach_InternetGateways = False
          if idDetail['VpcID']:
            print('Detaching {0} internet Gateway {1} from VPC ID {2}'.format(currentRegion, idDetail['DISPLAY_ID'], idDetail['VpcID']))
            try:
              ign = clientEC2Region.detach_internet_gateway(InternetGatewayId=id,VpcId=idDetail['VpcID'])
            except ClientError as e:
              print("    ERROR:", e, '\n')
              error_detach_InternetGateways = True
          if not error_detach_InternetGateways:
            print('Deleting {0} internet Gateway {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
            try:
              ign = clientEC2Region.delete_internet_gateway(InternetGatewayId=id)
            except ClientError as e:
              print("    ERROR:", e, '\n')


      #################################################################
      #  VPC delete
      #################################################################
      for currentRegion, idDict in termTrack.get(awsComponent.VPC, {}).items():
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        for id, idDetail in idDict.items():

          print('Deleting {0} VPC {1}'.format(currentRegion, idDetail['DISPLAY_ID']))
          #  Trap a couple simple error conditions. Provide warnings & explanation
          conflictList = []
          for idChk in clientEC2Region.describe_internet_gateways(Filters=[{'Name': 'attachment.vpc-id', 'Values': [id]}])['InternetGateways']:
            conflictList.append(idChk['InternetGatewayId'] + tagNameFind(idChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: {0} VPC {1} is attached to the following gateway(s):\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          conflictList = []
          for idChk in clientEC2Region.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [id]}])['Subnets']:
            conflictList.append(idChk['SubnetId']  + tagNameFind(idChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: {0} VPC {1} is associated with the following subnet(s):\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          conflictList = []
          for idChk in clientEC2Region.describe_vpc_endpoints(Filters=[{'Name': 'vpc-id', 'Values': [id]}])['VpcEndpoints']:
            conflictList.append(idChk['VpcEndpointId'])
          if conflictList:
            print('  WARNING: {0} VPC {1} is associated with the following endpoints:\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          conflictList = []
          for idChk in clientEC2Region.describe_route_tables(Filters=[{'Name': 'vpc-id', 'Values':[id]}])['RouteTables']:
            RouteTablesAssociations = chkRouteTablesAssociations(idChk['RouteTableId'], aws_cleanupArg, currentRegion)
            if not RouteTablesAssociations['Main']:
              conflictList.append(idChk['RouteTableId']  + tagNameFind(idChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: {0} VPC {1} is associated with the following route table(s):\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          conflictList = []
          for instResvChk in clientEC2Region.describe_instances(Filters=[{'Name': 'vpc-id', 'Values': [id]}])['Reservations']:
            for instChk in instResvChk['Instances']:
              conflictList.append(instChk['InstanceId'] + tagNameFind(instChk.get('Tags'), aws_cleanupArg))
          if conflictList:
            print('  WARNING: {0} VPC {1} is associated with the following EC2 instance(s):\n\t{2}'.format(currentRegion, id, '\n\t'.join(conflictList)))

          try:
            ign = clientEC2Region.delete_vpc(VpcId=id)
          except ClientError as e:
            print("    ERROR:", e, '\n')

      #################################################################
      #  S3 delete
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.S3, {}).items():
        #  Before a bucket can be deleted, the objects in the bucket first have to be
        #  deleted.
        print('Deleting any objects contained in S3 Bucket {0}...'.format(id))
        try:
          ign = resourceS3.Bucket(id).objects.delete()
        except ClientError as e:
          print("   ERROR: ", e, '\n')
        print('Deleting S3 Bucket {0}'.format(id))
        try:
          ign = resourceS3.Bucket(id).delete()
        except ClientError as e:
          print("   ERROR:", e, '\n')

      #################################################################
      #  VPC re-create (assuming to re-create by default)
      #################################################################
      print('Re-creating missing default VPCs...')
      for currentRegion in sorted(regions):
        clientEC2Region = boto3.client('ec2',region_name=currentRegion)
        #  Check to see the default VPC exists for this region
        isVPCDefault = False
        for chkVpc in clientEC2Region.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values':['true']}])['Vpcs']:
          isVPCDefault = True
        if isVPCDefault:
          print("\tRegion {0} - default VPC exists; no need to re-create".format(currentRegion).format(currentRegion))
        else:
          print("\tRegion {0} - re-creating VPC".format(currentRegion))
          try:
            ign = clientEC2Region.create_default_vpc()
          except ClientError as e:
            print("\t   ERROR:", e, '\n')
         

      #################################################################
      #  Users delete 
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.Users, {}).items():
        #  Before a user can be deleted, need to delete the access key and login profile.
        #  Remove access key from user (if it exists)
        dispItemsLine = dispItemsLineClass('User "{0}" ({1}) - deleting access key(s): '.format(id, idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_access_keys(UserName=id)['AccessKeyMetadata']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('AccessKeyId')), end = '')
            ign = clientIAM.delete_access_key(UserName = id, AccessKeyId = scanPrepDel.get('AccessKeyId'))
          except ClientError as e:
            print("   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        #  Remove login profile from user (if it exists)
        try:
          ign = clientIAM.get_login_profile(UserName = id)
          print('User "{0}" ({1}) - deleting login profile'.format(id, idDetail['DISPLAY_ID']))
          ign = clientIAM.delete_login_profile(UserName = id)
        except ClientError as e:
          if e.response['Error']['Code'] == 'NoSuchEntity':
            pass
          else:
            print("   ERROR:", e, '\n')

        #  Remove any groups granted to the user (required before deleting user)
        dispItemsLine = dispItemsLineClass('User "{0}" ({1}) - removing group(s): '.format(id, idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_groups_for_user(UserName=id)['Groups']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('GroupName')), end = '')
            ign = clientIAM.remove_user_from_group(GroupName = scanPrepDel.get('GroupName'), UserName=id)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        #  Remove any policies directly granted to the user (required before deleting user)
        dispItemsLine = dispItemsLineClass('User "{0}" ({1}) - detaching policies: '.format(id, idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_attached_user_policies(UserName=id)['AttachedPolicies']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('PolicyName')), end = '')
            ign = clientIAM.detach_user_policy(UserName=id, PolicyArn=scanPrepDel.get('PolicyArn'))
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        print('User "{0}" ({1}) - dropping account'.format(id, idDetail['DISPLAY_ID']))
        try:
          ign = clientIAM.delete_user(UserName=id)
        except ClientError as e:
          print("   ERROR:", e, '\n')

      #################################################################
      #  Groups delete 
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.Groups, {}).items():
        dispItemsLine = dispItemsLineClass('Group "{0}" - detaching users: '.format(id))
        for scanPrepDel in clientIAM.get_group(GroupName=id)['Users']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('UserName')), end = '')
            ign = clientIAM.remove_user_from_group(GroupName = id, UserName=scanPrepDel.get('UserName'))
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Group "{0}" - detaching policies: '.format(id))
        for scanPrepDel in clientIAM.list_attached_group_policies(GroupName=id)['AttachedPolicies']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('PolicyName')), end = '')
            ign = clientIAM.detach_group_policy(GroupName = id, PolicyArn=scanPrepDel.get('PolicyArn'))
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Group "{0}" - deleting inline policies: '.format(id))
        for scanPrepDel in clientIAM.list_group_policies(GroupName=id)['PolicyNames']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel), end = '')
            ign = clientIAM.delete_group_policy(GroupName = id, PolicyName = scanPrepDel)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        try:
          print('Group "{0}" - deleting'.format(id))
          ign = clientIAM.delete_group(GroupName=id)
        except ClientError as e:
          print("   ERROR:", e, '\n')
            
      #################################################################
      #  Policies delete
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.Policies, {}).items():
        dispItemsLine = dispItemsLineClass('Policy "{0}" - detaching groups: '.format(idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_entities_for_policy(PolicyArn=id)['PolicyGroups']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('GroupName')), end = '')
            ign = clientIAM.detach_group_policy(GroupName = scanPrepDel.get('GroupName'), PolicyArn = id)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Policy "{0}" - detaching users: '.format(idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_entities_for_policy(PolicyArn=id)['PolicyUsers']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('UserName')), end = '')
            ign = clientIAM.detach_user_policy(UserName = scanPrepDel.get('UserName'), PolicyArn = id)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Policy "{0}" - detaching roles: '.format(idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_entities_for_policy(PolicyArn=id)['PolicyRoles']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('RoleName')), end = '')
            ign = clientIAM.detach_role_policy(RoleName = scanPrepDel.get('RoleName'), PolicyArn = id)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Policy "{0}" - deleting non-default versions: '.format(idDetail['DISPLAY_ID']))
        for scanPrepDel in clientIAM.list_policy_versions(PolicyArn=id)['Versions']:
          if not scanPrepDel['IsDefaultVersion']:
            try:
              print(dispItemsLine.newItemName(scanPrepDel.get('VersionId')), end = '')
              ign = clientIAM.delete_policy_version(VersionId = scanPrepDel.get('VersionId'), PolicyArn = id)
            except ClientError as e:
              print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())
          
        try:
          print('Policy "{0}" - deleting'.format(idDetail['DISPLAY_ID']))
          ign = clientIAM.delete_policy(PolicyArn = id)
        except ClientError as e:
          print("   ERROR:", e, '\n')

      #################################################################
      #  Roles delete
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.Roles,{}).items():
        if not idDetail['IsAwsService']:
          dispItemsLine = dispItemsLineClass('Role "{0}" - detaching policies: '.format(id))
          for scanPrepDel in clientIAM.list_attached_role_policies(RoleName=id)['AttachedPolicies']:
            try:
              print(dispItemsLine.newItemName(scanPrepDel.get('PolicyName')), end = '')
              ign = clientIAM.detach_role_policy(RoleName = id, PolicyArn=scanPrepDel.get('PolicyArn'))
            except ClientError as e:
              print("\n   ERROR:", e, '\n')
          print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Role "{0}" - deleting inline policies: '.format(id))
        for scanPrepDel in clientIAM.list_role_policies(RoleName=id)['PolicyNames']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel), end = '')
            ign = clientIAM.delete_role_policy(RoleName = id, PolicyName=scanPrepDel)
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        dispItemsLine = dispItemsLineClass('Role "{0}" - removing instance profile(s): '.format(id))
        for scanPrepDel in clientIAM.list_instance_profiles_for_role(RoleName=id)['InstanceProfiles']:
          try:
            print(dispItemsLine.newItemName(scanPrepDel.get('InstanceProfileName')), end = '')
            ign = clientIAM.remove_role_from_instance_profile(RoleName = id, InstanceProfileName=scanPrepDel.get('InstanceProfileName'))
          except ClientError as e:
            print("\n   ERROR:", e, '\n')
        print("", end = dispItemsLine.EOL())

        if idDetail['IsAwsService']:
          try:
            print('Role "{0}" - deleting service linked role'.format(id))
            ign = clientIAM.delete_service_linked_role(RoleName = id)
          except ClientError as e:
            print("   ERROR:", e, '\n')
        else:
          try:
            print('Role "{0}" - deleting'.format(id))
            ign = clientIAM.delete_role(RoleName = id)
          except ClientError as e:
            print("   ERROR:", e, '\n')

      #################################################################
      #  InstanceProfiles delete
      #################################################################
      for id, idDetail in termTrack.get(awsComponent.InstanceProfiles, {}).items():
        try:
          print('Instance profile "{0}" - deleting'.format(id))
          ign = clientIAM.delete_instance_profile(InstanceProfileName = id)
        except ClientError as e:
          print("   ERROR:", e, '\n')
    else:
      print('Invalid Verification Code entered. Exiting script WITHOUT terminating/deleting AWS components')
