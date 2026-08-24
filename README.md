# openIMIS Backend social_protection reference module
This repository holds the files of the openIMIS Backend social_protection reference module.
It is dedicated to be deployed as a module of [openimis-be_py](https://github.com/openimis/openimis-be_py).

## ORM mapping:
* social_protection_benefitplan, social_protection_historicalbenefitplan > BenefitPlan
* social_protection_beneficiary, social_protection_historicalbeneficiary > Beneficiary
* social_protection_benefitplandatauploadsrecords, social_protection_historicalbenefitplandatauploadsrecords > BenefitPlanDataUploadRecords
* social_protection_groupbeneficiary, social_protection_historicalgroupbeneficiary > GroupBeneficiary

## GraphQl Queries
* benefitPlan
* beneficiary
* groupBeneficiary
* beneficiaryDataUploadHistory
* bfCodeValidity
* bfNameValidity
* bfNameValidity
* bfSchemaValidity
* beneficiaryExport
* groupBeneficiaryExport

## GraphQL Mutations - each mutation emits default signals and return standard error lists (cfr. openimis-be-core_py)
* createBenefitPlan
* updateBenefitPlan
* deleteBenefitPlan
* createBeneficiary
* updateBeneficiary
* deleteBeneficiary
* createGroupBeneficiary
* updateGroupBeneficiary
* deleteGroupBeeficiary

## Services
- BenefitPlan
  - create
  - update
  - delete
  - create_update_task
- Beneficiary
  - create
  - update
  - delete
- GroupBeneficiary
  - create
  - update
  - delete
- BeneficiaryImport
  - import_beneficiaries

## Configuration options (can be changed via core.ModuleConfiguration)
* gql_benefit_plan_search_perms: required rights to call benefitPlan GraphQL Query (default: ["160001"])
* gql_benefit_plan_create_perms: required rights to call createBenefitPlan GraphQL Mutation (default: ["160002"])
* gql_benefit_plan_update_perms: required rights to call updateBenefitPlan GraphQL Mutation (default: ["160003"])
* gql_benefit_plan_delete_perms: required rights to call deleteBenefitPlan GraphQL Mutation (default: ["160004"])
* gql_beneficiary_search_perms: required rights to call beneficiary and groupBeneficiary GraphQL Mutation (default: ["170001"])
* gql_beneficiary_create_perms: required rights to call createBeneficiary and createGroupBeneficiary GraphQL Mutation (default: ["160002"])
* gql_beneficiary_update_perms: required rights to call updateBeneficiary and updateGroupBeneficiary GraphQL Mutation (default: ["160003"])
* gql_beneficiary_delete_perms: required rights to call deleteBeneficiary and deleteGroupBeneficiary GraphQL Mutation (default: ["170004"])
* gql_benefit_plan_criteria_search_perms: required rights to read a Benefit Plan's sanitized `advancedCriteria` field (default: ["171005"])
* gql_benefit_plan_criteria_update_perms: required rights to explicitly create, change, or clear `json_ext.advanced_criteria` (default: ["171006"])
* benefit_plan_creation_defaults: validated defaults copied into each new Benefit Plan. Values from `common` are recursively merged with `INDIVIDUAL` or `GROUP` values and then with authorized request values. Supported fields are `beneficiary_data_schema`, `json_ext`, `max_beneficiaries`, `ceiling_per_beneficiary`, `institution`, and `description`. `json_ext.enrolment_ranking` accepts per-status (or `*`) deterministic `order_by`, `tie_breaker`, and percentage/max-beneficiary limit rules. Configuration changes apply only to future Benefit Plans.
* mandatory_enrollment_criteria: validated live system rules keyed by `INDIVIDUAL`/`GROUP` and beneficiary status. They are evaluated on every enrollment and cannot be removed by editing a Benefit Plan or changing the browser payload (default: empty rules).

* gql_check_benefit_plan_update: specifies whether Benefit Plan update should be updated using task based approval (default: True)
* gql_check_beneficiary_crud: specifies whether Beneficiary CRUD should be use task based approval (default: True)
* gql_check_group_beneficiary_crud: specifies whether Group Beneficiary should use tasks based approval (default: True),


## openIMIS Modules Dependencies
- core
- individual

## OpenSearch

### Available Documents 
* BeneficiaryDocument

### How to initlaize data after deployment
* If you have initialized the application but still have some data to be transferred, you can effortlessly 
achieve this by using the commands available in this module: `python manage.py add_beneficiary_data_to_opensearch`. 
This command loads existing data into OpenSearch.

### How to Import a Dashboard
* Locate the dashboard definition file in `.ndjson` format within 
the `openimis-be_social_protection/import_data` directory.
* Log in to your OpenSearch instance.
* Expand the sidebar located on the left side of the page.
* Navigate to `Management` and select `Dashboards Management`.
* On the left side of the page, click on `Saved Objects`.
* At the top-right corner of the table, click on `Import`.
* A new side-modal will appear on the right side of the page. 
Drag and drop the file from `openimis-be_social_protection/import_data` into the import dropzone.
* This action will import the dashboards along with related 
charts that should be accessible on the visualization page.
* Verify if the dashboards have been imported properly.

### File for importing in .ndsjon format
* This file contains dashboard definitions that can be easily uploaded, as described in the "How to Import a Dashboard" 
section above. It includes definitions of dashboards and the visualizations contained within them.

### How to Export Dashboards with Related Objects like Visualizations in OpenSearch?
* Log in to your OpenSearch instance.
* Expand the sidebar located on the left side of the page.
* Navigate to `Management` and select `Dashboards Management`.
* On the left side of the page, click on `Saved Objects`.
* At the top-right corner of the table, click on `Export <N> objects`.
* Ensure that you have selected dashboards only. Additionally, choose the option to 
include related objects, and then click export all.
* You should have downloaded file in `.ndjson` format. 
* Save file in the business model for initialization after deployment in 
`openimis-be_social_protection/import_data`.
* Rename filename into `opensearch_beneficiary_dashboard.ndjson`

## Validations and deduplication detection

### Validations endpoint
* This is handled by the POST endpoint 'api/social_protection/validate_import_beneficiaries'.
* The endpoint is utilized within the upload workflow when a user uploads beneficiaries into a specific system.
* The input required is identical to that of the POST endpoint 'api/social_protection/import_beneficiaries' (CSV file). 
* The endpoint heavily relies on schema properties. For instance, `validationCalculation` in the schema triggers a specific validation strategy. Similarly, in the duplications section of the schema, 
setting `uniqueness: true` signifies the need for duplication checks based on the record's field value.
* Based on the provided schema below (from `programme/benefit plan`), it indicates that validations will run for the `email` 
field (`validationCalculation`), and duplication checks will be performed for `national_id` (`uniqueness: true`)
```
{
   "$id":"https://example.com/beneficiares.schema.json",
   "type":"object",
   "title":"Record of beneficiares",
   "$schema":"http://json-schema.org/draft-04/schema#",
   "properties":{
      "email":{
         "type":"string",
         "description":"email address to contact with beneficiary",
         "validationCalculation":{
            "name":"EmailValidationStrategy"
         }
      },
      "able_bodied":{
         "type":"boolean",
         "description":"Flag determining whether someone is able bodied or not"
      },
      "national_id":{
         "type":"string",
         "uniqueness":true,
         "description":"national id"
      },
      "educated_level":{
         "type":"string",
         "description":"The level of person when it comes to the school/education/studies"
      },
      "chronic_illness":{
         "type":"boolean",
         "description":"Flag determining whether someone has such kind of illness or not"
      },
      "national_id_type":{
         "type":"string",
         "description":"A type of national id"
      },
      "number_of_elderly":{
         "type":"integer",
         "description":"Number of elderly"
      },
      "number_of_children":{
         "type":"integer",
         "description":"Number of children"
      },
      "beneficiary_data_source":{
         "type":"string",
         "description":"The source from where such beneficiary comes"
      }
   },
   "description":"This document records the details beneficiares"
}
```
* An example response after calling the endpoint looks like this:
```
{
   "success":true,
   "data":[
      {
         "row":{
            "first_name":"Rick",
            "last_name":"Scott",
            "dob":"2023-07-13",
            "email":"testtesttest@test.com",
            "able_bodied":false,
            "national_id":"1345320000AN",
            "educated_level":"higher education",
            "national_id_type":"National ID Card",
            "number_of_elderly":1,
            "number_of_children":2,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":true,
               "field_name":"email",
               "note":"Ok",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":false,
               "field_name":"national_id",
               "note":"'national_id' Field value '1345320000AN' is duplicated",
               "duplications":{
                  "duplicated":true,
                  "duplicates_amoung_database":[
                     {
                        "id":"dbe11b3d-c6db-4912-bc84-c8e3d57afdb7",
                        "first_name":"TestFN",
                        "last_name":"TestLN",
                        "dob":"2023-07-13",
                        "email":"testtesttest@test.com",
                        "able_bodied":false,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"National ID Card",
                        "number_of_elderly":1,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"8321950f-a017-4940-a7ac-977714b685ec",
                        "first_name":"Lewis",
                        "last_name":"Test",
                        "dob":"1998-06-04",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":0,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"bc0c2772-fcfa-46a4-9b42-894707db2c37",
                        "first_name":"Jacob",
                        "last_name":"Open",
                        "dob":"1995-06-01",
                        "able_bodied":false,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     },
                     {
                        "id":"ea21f84c-28db-4039-96d4-460a96bb2278",
                        "first_name":"Jacob",
                        "last_name":"Open",
                        "dob":"1995-06-01",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"higher education",
                        "national_id_type":"passport",
                        "number_of_elderly":0,
                        "number_of_children":4,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     }
                  ],
                  "incoming_duplicates":[
                     {
                        "first_name":"Eva",
                        "last_name":"Jacob",
                        "dob":"1995-06-01",
                        "email":"22121211221dsdsdsds2@gmail.com",
                        "able_bodied":true,
                        "national_id":"1345320000AN",
                        "educated_level":"secondary education",
                        "national_id_type":"National ID Card",
                        "number_of_elderly":1,
                        "number_of_children":2,
                        "beneficiary_data_source":"BENEFICIARY_ETL"
                     }
                  ]
               }
            }
         }
      },
      {
         "row":{
            "first_name":"Frank",
            "last_name":"Mood",
            "dob":"1995-06-01",
            "email":"frank.mood@test.com",
            "able_bodied":true,
            "national_id":"134532022LKSD",
            "educated_level":"medium education",
            "national_id_type":"National ID Card",
            "number_of_elderly":0,
            "number_of_children":1,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":true,
               "field_name":"email",
               "note":"Ok",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":true,
               "field_name":"national_id",
               "note":"'national_id' Field value '134532022LKSD' is not duplicated",
               "duplications":null
            }
         }
      },
      {
         "row":{
            "first_name":"Jan",
            "last_name":"White",
            "dob":"1995-06-01",
            "email":"janwhitetest.com",
            "able_bodied":true,
            "national_id":"1345320000ANER",
            "educated_level":"higher education",
            "national_id_type":"National ID Card",
            "number_of_elderly":0,
            "number_of_children":4,
            "beneficiary_data_source":"BENEFICIARY_ETL"
         },
         "validations":{
            "email":{
               "success":false,
               "field_name":"email",
               "note":"Invalid email format",
               "duplications":null
            },
            "national_id_uniqueness":{
               "success":true,
               "field_name":"national_id",
               "note":"'national_id' Field value '1345320000ANER' is not duplicated",
               "duplications":null
            }
         }
      },
   ]
}
```
* Within the example response, the `data` section contains information about each row in an array format.
* Each element in the array is a dictionary representing a row from the input CSV file. 
* Inside this dictionary, the `row` key holds the representation of a new individual/beneficiary entering the system with provided values.
* Under `validations`, you'll find validated fields (if the field in the schema is marked by a validation class) and potential duplicates if 
`uniqueness` property is set for the field.  
If the property `uniqueness` is set for a particular field, in `validations`, an additional key suffix `_uniqueness` indicates potential duplicates. 
* The 'duplication' section shows potential duplicates among incoming (`incoming_duplicates`) and existing records (`duplicates_amoung_database`).
* An empty `validation` property indicates that no validations need processing based on the schema properties. 
* Next to the `data` and `success` properties, there is a `summary_invalid_items` field containing a list of uuids of individual data sources which are invalid. 
This list is necessary in the Benefit Update workflow to flag such records in the IndividualDataSource.

### Validations in upload workflow
* https://github.com/openimis/openimis-lightning_dkr/tree/develop Here there are two workflows responsible for uploading and validation data: `BenefitPlanUpdate` and `beneficiary-import-valid-items`
* The workflow `BenefitPlanUpdate` is utilized when a file is uploaded using a form in BenefitPlanPage.
* The workflow named `beneficiary-import-valid-items` is activated to confirm valid items following the validation process. Its activation occurs when a task linked to that specific action is initiated.
* The validation operates according to the calculation rule, defining the strategy for determining validation approaches.
* More about calculation strategy verification in calcrule strategy you can find more in [README Section in calculation validation strategy module](https://github.com/openimis/openimis-be-calcrule_validations_py/tree/develop) 
* The upload process involves two stages: first, a validation process verifies the data, and upon successful validation, 
the data is uploaded. In case of any invalid items, there's an additional step where the user can review and download a 
report containing the invalid items. After reviewing the report, the user can proceed to import the valid items through task management.
* For successful scenarios, the status is marked as `SUCCESS`. There's no requirement for maker-checker validation (task) since the process wasn't halted.
* In scenarios where one or more records are invalid, the status is `WAITING_FOR_VERIFICATION`. This indicates the presence of a task in the maker-checker view for verifying the upload of valid items. 
The report containing invalid items can be downloaded from the upload history on the benefit plan page.
* When a user accepts the valid items from an import that faced issues with some invalid items and there are no errors in this workflow, 
the status of the import is marked as `PARTIAL_SUCCESS`. This triggers the `beneficiary-import-valid-items` workflow in such cases. 


### Enabling Python Workflows
Module comes with simple workflows for data upload. 
They should be used for the development purposes, not in production environment. 
To activate these Python workflows, a configuration change is required. 
Specifically, the `enable_python_workflows` parameter to `true` within module config.

Workflows: 
 * beneficiary upload
