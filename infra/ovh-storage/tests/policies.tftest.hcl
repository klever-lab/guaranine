mock_provider "ovh" {}

variables {
  project_id   = "0123456789abcdef0123456789abcdef"
  state_bucket = "guaranine-test-state"
}

override_resource {
  target = ovh_cloud_project_user.storage["books-owner"]
  values = { id = "1001" }
}

override_resource {
  target = ovh_cloud_project_user.storage["books-app"]
  values = { id = "1002" }
}

override_resource {
  target = ovh_cloud_project_user.storage["guaranine-state"]
  values = { id = "1003" }
}

run "scoped_storage" {
  command = apply

  assert {
    condition = (
      ovh_cloud_project_storage.books.owner_id == 1001 &&
      ovh_cloud_project_storage.books.versioning.status == "enabled" &&
      ovh_cloud_project_storage.books.encryption.sse_algorithm == "AES256" &&
      ovh_cloud_project_user_s3_policy.books.user_id == "1002" &&
      ovh_cloud_project_user_s3_policy.state.user_id == "1003"
    )
    error_message = "Books requires an encrypted versioned bucket and a distinct owner and workload user. The state policy belongs to the backend user."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(ovh_cloud_project_user_s3_policy.books.policy).Statement :
      alltrue([
        for arn in statement.Resource :
        contains([local.books_arn, "${local.books_arn}/*"], arn)
      ]) && !contains(statement.Action, "s3:DeleteObject")
      if statement.Effect == "Allow"
    ])
    error_message = "Books can only read and publish in its bucket."
  }

  assert {
    condition = alltrue([
      for statement in jsondecode(ovh_cloud_project_user_s3_policy.state.policy).Statement :
      alltrue([
        for arn in statement.Resource :
        contains([local.state_arn, local.state_key, local.state_lock], arn) &&
        (!contains(statement.Action, "s3:DeleteObject") || arn == local.state_lock)
      ])
      if statement.Effect == "Allow"
    ])
    error_message = "The backend user can only access its state bucket and may delete only the lock object."
  }

  assert {
    condition = (
      one([
        for statement in jsondecode(ovh_cloud_project_user_s3_policy.state.policy).Statement :
        one(statement.Resource)
        if statement.Effect == "Allow" && contains(statement.Action, "s3:DeleteObject")
      ]) == local.state_lock &&
      one([
        for statement in jsondecode(ovh_cloud_project_user_s3_policy.state.policy).Statement :
        one(statement.Resource)
        if statement.Effect == "Deny" && contains(statement.Action, "s3:DeleteObject")
      ]) == local.state_key
    )
    error_message = "The backend must be able to remove its lock and have an explicit deny on state object deletion."
  }

  assert {
    condition = alltrue([
      for policy in [ovh_cloud_project_user_s3_policy.state.policy, ovh_cloud_project_user_s3_policy.books.policy] :
      anytrue([
        for statement in jsondecode(policy).Statement :
        statement.Effect == "Deny" &&
        contains(statement.Resource, "*") &&
        contains(statement.Action, "s3:ListAllMyBuckets") &&
        contains(statement.Action, "s3:CreateBucket")
      ])
    ])
    error_message = "Each scoped user must be explicitly denied account-wide listing and bucket creation."
  }
}
