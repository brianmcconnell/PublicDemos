# Databricks notebook source
# DBTITLE 1,Create Genie Space from JSON file via API
import json
import requests
import os

# Step 1: Read the JSON file Change the filepath to your file
file_path = '/Workspace/Users/dr.b.mcconnell@gmail.com/FinOpsGenie/AWS_GCP_cost_reporting_genie_space_v2.0.json'
with open(file_path, 'r') as f:
    genie_payload = json.load(f)

# Step 2: Get Databricks host and API token using dbutils
DATABRICKS_HOST = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiUrl().get()
DATABRICKS_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()

url = f"{DATABRICKS_HOST}/api/2.0/genie/spaces"
headers = {
    'Authorization': f'Bearer {DATABRICKS_TOKEN}',
    'Content-Type': 'application/json',
}

# Step 3: Make the POST request
response = requests.post(url, headers=headers, json=genie_payload)

# Step 4: Print the response for verification
print('Status code:', response.status_code)
print('Response:', response.json())

