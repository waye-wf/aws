# aws_cleanup.py
**Current AWS components in-scope for aws_cleanup.py:**
- EC2 instances 
- Security Groups 
- Volumes 
- Key Pairs 
- Metric Alarms
- Config Rules 
- Configuration Recorder 
- CloudFormation Stacks
- Cloud Trail 
- Cloud Watch Log Group 
- SNS Topic
- S3 Buckets 
- VPC 
- Subnets 
- Internet Gateways 
- Route Tables 
- VPC Endpoints 
- User 
- Group 
- Policy 
- Role
- Instance Profile

## Running aws_cleanup.py
Both aws_cleanup.py AND aws_cleanup_import.py files need to be in the same directory
- **INVENTORY OF AWS COMPONENTS (_no deletion_):**
  - **``# python3 aws_cleanup.py``**  
    Run without parameters, aws_cleanup.py displays an inventory of AWS components for all regions. 
    - Column "keep(Tag)" identifies which AWS items have the tag key "keep". 
    - Column "keep" is for AWS components that don't have tag keys and are flagged in the aws_cleanup_import.py file (see Advanced Settings below). When "aws_cleanup.py --del" is run, items flagged as "keep" are not deleted.

  
- **DELETING AWS COMPONENTS:**
  - **``# python3 aws_cleanup.py --del``**  
    Deletes all AWS components except for items identified as "keep" and Default VPCs. The script will first show an inventory of which AWS items will be terminated/deleted, followed by a confirmation prompt.
  - **``# python3 aws_cleanup.py --del --vpc_rebuild``**   
    Deletes all AWS components except for items with the "keep" tag, and deletes/recreates all Default VPCs. The recreated Default VPCs will be the same configuration as new AWS setup. The script will first list an inventory of which AWS items will be terminated/deleted, followed by a confirmation prompt.
  


## Advanced Settings:
**The file aws_cleanup_import.py contains script control settings that can be modified by the end-user.**
- **``constantKeepTag = ['keep']``**  
  Python list of tag keys used to flag AWS items from being deleted.  Can have multiple case-insensitive entries.  
  Ex: to replace the default 'keep' flag with 'no_delete' and include tag key 'wfw' for blocking, change constantKeepTag to the following:  
    ``constantKeepTag =['no_delete', 'wfw']``
    
- **``self.EC2 = componentDef(compName = 'EC2 instances', compDelete = True)``**  
  **``self.SecGroup = componentDef(compName = 'Security Groups', compDelete = True)``**  
  **``self.Volume = componentDef(compName = 'Volumes', compDelete = True)``**  
  **``self.KeyPairs = componentDef(compName = 'Key Pairs', compDelete = True)``**  
  **``self.User = componentDef(compName = 'User', compDelete = True, itemsKeep=())``**  
  **``self.Group = componentDef(compName = 'Group', compDelete = True, itemsKeep=())``**  
  **``...``**
  
  List of AWS components that aws_cleanup.py script can inventory/delete  
  Fields:  
  - **compName**: AWS component Name; no need to change.
  - **compDelete**: flag to block entire AWS component from being deleted. **True** to allow AWS component deletion, **False** to block deletion (case sensitive!).
  - **itemsKeep**: where AWS components don't have tags (key pairs, users, policies, etc), itemsKeep is a quoted list of item names not to delete. Ex: itemsKeep=('Seattle', 'Redmond')
  
  Examples: 
  - To prevent all Key Pairs from being deleted, change KeyPair's compDelete from True to False (case sensitive!):  
    ``self.KeyPairs = componentDef(compName = 'Key Pairs', ``**``compDelete = False``**``)``
  - To prevent user Ann from being deleted, change itemsKeep to the following (trailing comma is requred in the list!):
    ``self.KeyPairs = componentDef(compName = 'User', compDelete = False, ``**``itemsKeep = ('ann',)``**``)``
  - To prevent user Ann and Scott from being deleted, change itemsKeep to the following:
    ``self.KeyPairs = componentDef(compName = 'User', compDelete = False, ``**``itemsKeep = ('ann','scott')``**``)``

