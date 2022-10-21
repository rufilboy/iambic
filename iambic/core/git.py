import os
import re
from io import StringIO
from typing import Optional

from deepdiff import DeepDiff
from git import Repo
from pydantic import BaseModel as PydanticBaseModel

from iambic.config.models import Config
from iambic.config.templates import TEMPLATE_TYPE_MAP
from iambic.core.logger import log
from iambic.core.models import Deleted
from iambic.core.utils import NOQ_TEMPLATE_REGEX, file_regex_search, yaml


class GitDiff(PydanticBaseModel):
    path: str
    content: Optional[str] = None
    is_deleted: Optional[bool] = False


async def retrieve_git_changes(repo_dir: str) -> dict[str, list[GitDiff]]:
    repo = Repo(repo_dir)
    # Last commit of the current branch
    commit_feature = repo.head.commit.tree
    # Comparing against main
    commit_origin_main = repo.commit("origin/main")
    diff_index = commit_origin_main.diff(commit_feature)
    files = {
        "new_files": [],
        "deleted_files": [],
        "modified_files": [],
    }

    # Collect all new files
    for file_obj in diff_index.iter_change_type("A"):
        if (path := file_obj.b_path).endswith(".yaml") and (
            await file_regex_search(path, NOQ_TEMPLATE_REGEX)
        ):
            file = GitDiff(path=str(os.path.join(repo_dir, path)))
            files["new_files"].append(file)

    # Collect all deleted files
    for file_obj in diff_index.iter_change_type("D"):
        if (path := file_obj.b_path).endswith(".yaml"):
            file = GitDiff(
                path=str(os.path.join(repo_dir, path)),
                content=file_obj.a_blob.data_stream.read().decode("utf-8"),
                is_deleted=True,
            )
            if re.search(NOQ_TEMPLATE_REGEX, file.content):
                files["deleted_files"].append(file)

    # Collect all modified files
    for file_obj in diff_index.iter_change_type("M"):
        if (path := str(os.path.join(repo_dir, file_obj.b_path))).endswith(
            ".yaml"
        ) and (await file_regex_search(path, NOQ_TEMPLATE_REGEX)):
            if (main_path := file_obj.a_path) != path:  # File was renamed
                deleted_file = GitDiff(
                    path=str(os.path.join(repo_dir, main_path)),
                    content=file_obj.a_blob.data_stream.read().decode("utf-8"),
                    is_deleted=True,
                )

                if re.search(NOQ_TEMPLATE_REGEX, deleted_file.content):
                    template_dict = yaml.load(open(path))
                    main_template_dict = yaml.load(StringIO(deleted_file.content))
                    if not DeepDiff(
                        template_dict, main_template_dict, ignore_order=True
                    ):
                        continue  # Just renamed but no file changes

                    template_cls = TEMPLATE_TYPE_MAP[
                        main_template_dict["template_type"]
                    ]
                    main_template = template_cls(
                        file_path=deleted_file.path, **main_template_dict
                    )
                    template = template_cls(file_path=path, **template_dict)
                    if main_template.resource_name != template.resource_name:
                        files["deleted_files"].append(deleted_file)
                        files["new_files"].append(GitDiff(path=path))
                        continue

            file = GitDiff(
                path=str(os.path.join(repo_dir, path)),
                content=file_obj.a_blob.data_stream.read().decode("utf-8"),
            )
            files["modified_files"].append(file)

    return files


def create_templates_for_deleted_files(deleted_files: list[GitDiff]) -> list:
    """
    Create a class instance of the deleted file content with its template type
    If it wasn't deleted, set it to deleted
    Add that instance to templates
    """
    templates = []
    for git_diff in deleted_files:
        template_dict = yaml.load(StringIO(git_diff.content))
        template_cls = TEMPLATE_TYPE_MAP[template_dict["template_type"]]
        template = template_cls(file_path=git_diff.path, **template_dict)
        if template.deleted is True:
            continue
        template.deleted = True
        log.info("Template marked as deleted", file_path=git_diff.path)
        templates.append(template)

    return templates


def create_templates_for_modified_files(
    config: Config, modified_files: list[GitDiff]
) -> list:

    """
    Create a class instance of the original file content and the new file content with its template type
    Check for accounts that were removed from included_accounts or added to excluded_accounts
    Update the template to be applied to delete the role from the accounts that hit on the above statement
    """
    templates = []
    for git_diff in modified_files:
        main_template_dict = yaml.load(StringIO(git_diff.content))
        template_cls = TEMPLATE_TYPE_MAP[main_template_dict["template_type"]]

        main_template = template_cls(file_path=git_diff.path, **main_template_dict)

        template_dict = yaml.load(open(git_diff.path))
        template = template_cls(file_path=git_diff.path, **template_dict)
        deleted_included_accounts = []
        # deleted_exclude_accounts are accounts that are included in the current commit so can't be deleted
        deleted_exclude_accounts = [*template.included_accounts]
        deleted_exclude_accounts_str = "\n".join(deleted_exclude_accounts)

        # Catch accounts that were in included accounts but have been removed
        if "*" not in deleted_exclude_accounts:
            if "*" in main_template.included_accounts:
                """
                Catch accounts that were implicitly removed from included_accounts.
                Example:
                    main branch included_accounts:
                        - *
                    current commit included_accounts:
                        - staging
                        - dev

                If config.accounts included prod, staging, and dev this will catch that prod is no longer included.
                    This means marking prod for deletion as it has been implicitly deleted.
                """
                for account_config in config.accounts:
                    account_regex = (
                        rf"({account_config.account_id}|{account_config.account_name})"
                    )
                    if re.search(account_regex, deleted_exclude_accounts_str):
                        log.debug(
                            "Resource on account not marked deletion.",
                            account=account_regex,
                            template=git_diff.path,
                        )
                        continue

                    log.info(
                        "Marking resource for deletion on account.",
                        reason="Implicitly removed from included_accounts",
                        account=account_regex,
                        template=git_diff.path,
                    )
                    deleted_included_accounts.append(account_regex)
                    template.included_accounts.append(account_regex)
            else:
                """
                Catch accounts that were explicitly removed from included_accounts.
                Example:
                    main branch included_accounts:
                        - prod
                        - staging
                        - dev
                    current commit included_accounts:
                        - staging
                        - dev

                This means marking prod for deletion as it has been implicitly deleted.
                """
                for account in main_template.included_accounts:
                    if re.search(account, deleted_exclude_accounts_str):
                        log.debug(
                            "Resource on account not marked deletion.",
                            account=account,
                            template=git_diff.path,
                        )
                        continue

                    log.info(
                        "Marking resource for deletion on account.",
                        reason="Explicitly removed from included_accounts.",
                        account=account,
                        template=git_diff.path,
                    )
                    deleted_included_accounts.append(account)
                    template.included_accounts.append(account)

        main_template_included_accounts_str = "\n".join(main_template.included_accounts)
        main_template_excluded_accounts_str = (
            "\n".join(main_template.excluded_accounts)
            if main_template.excluded_accounts
            else None
        )
        template_excluded_accounts = []
        """
        Catch accounts that have been implicitly excluded.
        Example:
            included_accounts:
                - *
            main branch excluded_accounts: []
            current commit excluded_accounts:
                - prod

        If config.accounts included prod, staging, and dev this will catch that prod is no longer included.
            This means marking prod for deletion as it has been implicitly deleted.
        """
        for account in template.excluded_accounts:
            if main_template_excluded_accounts_str and re.search(
                account, main_template_excluded_accounts_str
            ):
                # The account was already excluded so add it to the template_excluded_accounts
                log.debug(
                    "Resource already excluded on account.",
                    account=account,
                    template=git_diff.path,
                )
                template_excluded_accounts.append(account)
            elif (
                re.search(account, main_template_included_accounts_str)
                or "*" in main_template.included_accounts
            ):
                # The account was previously included so mark it for deletion
                log.info(
                    "Marking resource for deletion on account.",
                    reason="Account added to excluded_accounts for resource.",
                    account=account,
                    template=git_diff.path,
                )
                deleted_included_accounts.append(account)
                template.included_accounts.append(account)
            else:
                # The account wasn't included or excluded before this so add it back to template_excluded_accounts
                log.debug(
                    "Newly excluded account.", account=account, template=git_diff.path
                )
                template_excluded_accounts.append(account)

        template.excluded_accounts = template_excluded_accounts

        if deleted_included_accounts and template.deleted is not True:
            deleted_obj = Deleted(
                deleted=True,
                included_accounts=deleted_included_accounts,
                excluded_accounts=[ea for ea in deleted_exclude_accounts if ea != "*"],
            )
            if isinstance(template.deleted, bool):
                template.deleted = [deleted_obj]
            else:
                template.deleted.append(deleted_obj)

        templates.append(template)

    return templates
