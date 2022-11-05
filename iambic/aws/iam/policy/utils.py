import asyncio

from botocore.exceptions import ClientError
from deepdiff import DeepDiff

from iambic.aws.utils import paginated_search
from iambic.core import noq_json as json
from iambic.core.context import ExecutionContext
from iambic.core.logger import log
from iambic.core.models import ProposedChange, ProposedChangeType
from iambic.core.utils import NoqSemaphore, aio_wrapper


async def list_managed_policy_versions(policy_arn: str, iam_client) -> list[dict]:
    return (
        await aio_wrapper(iam_client.list_policy_versions, PolicyArn=policy_arn)
    ).get("Versions", [])


async def list_managed_policy_tags(managed_policy_arn: str, iam_client) -> list[dict]:
    return await paginated_search(
        iam_client.list_policy_tags, "Tags", PolicyArn=managed_policy_arn
    )


async def get_managed_policy_version_doc(
    managed_policy_arn: str, version_id: str, iam_client, **kwargs
) -> dict:
    return (
        (
            await aio_wrapper(
                iam_client.get_policy_version,
                PolicyArn=managed_policy_arn,
                VersionId=version_id,
            )
        )
        .get("PolicyVersion", {})
        .get("Document", {})
    )


async def get_managed_policy(managed_policy_arn: str, iam_client, **kwargs) -> dict:
    try:
        response = (
            await aio_wrapper(iam_client.get_policy, PolicyArn=managed_policy_arn)
        ).get("Policy", {})
        if response:
            response["PolicyDocument"] = await get_managed_policy_version_doc(
                managed_policy_arn, response.pop("DefaultVersionId"), iam_client
            )
    except ClientError as err:
        if err.response["Error"]["Code"] == "NoSuchEntity":
            response = {}
        else:
            raise

    return response


def get_oldest_policy_version_id(policy_versions: list[dict]) -> str:
    policy_versions = sorted(policy_versions, key=lambda version: version["CreateDate"])
    if not policy_versions[0]["IsDefaultVersion"]:
        return policy_versions[0]["VersionId"]
    elif len(policy_versions) > 1:
        return policy_versions[1]["VersionId"]


async def list_managed_policies(
    iam_client,
    scope: str = "Local",
    only_attached: bool = False,
    path_prefix: str = "/",
    policy_usage_filter: str = None,
):
    get_managed_policy_semaphore = NoqSemaphore(get_managed_policy, 50)
    list_policy_kwargs = dict(
        Scope=scope,
        OnlyAttached=only_attached,
        PathPrefix=path_prefix,
    )
    if policy_usage_filter:
        list_policy_kwargs["PolicyUsageFilter"] = policy_usage_filter

    managed_policies = await paginated_search(
        iam_client.list_policies, response_key="Policies", **list_policy_kwargs
    )
    return await get_managed_policy_semaphore.process(
        [
            {"managed_policy_arn": policy["Arn"], "iam_client": iam_client}
            for policy in managed_policies
        ]
    )


async def delete_managed_policy(policy_arn: str, iam_client):
    await aio_wrapper(iam_client.delete_policy, PolicyArn=policy_arn)


async def update_managed_policy(
    policy_arn: str,
    iam_client,
    template_policy_document: dict,
    existing_policy_document: dict,
    read_only: bool,
    log_params: dict,
    context: ExecutionContext,
) -> list[ProposedChange]:
    response = []
    if isinstance(existing_policy_document, str):
        existing_policy_document = json.loads(existing_policy_document)
    policy_drift = await aio_wrapper(
        DeepDiff,
        existing_policy_document,
        template_policy_document,
        ignore_order=True,
        report_repetition=True,
    )

    if policy_drift:
        log_str = "Changes to the PolicyDocument discovered."
        response.append(
            ProposedChange(
                change_type=ProposedChangeType.UPDATE,
                attribute="policy_document",
                change_summary=policy_drift,
                current_value=existing_policy_document,
                new_value=template_policy_document,
            )
        )

        if not read_only:
            if policy_drift:
                policy_versions = await list_managed_policy_versions(
                    policy_arn, iam_client
                )
                if len(policy_versions) == 5:
                    await aio_wrapper(
                        iam_client.delete_policy_version,
                        PolicyArn=policy_arn,
                        VersionId=get_oldest_policy_version_id(policy_versions),
                    )

            log_str = f"{log_str} Updating PolicyDocument..."
            await aio_wrapper(
                iam_client.create_policy_version,
                PolicyArn=policy_arn,
                PolicyDocument=json.dumps(template_policy_document),
            )

        log.info(log_str, **log_params)
    return response


async def apply_managed_policy_tags(
    policy_arn: str,
    iam_client,
    template_tags: list[dict],
    existing_tags: list[dict],
    read_only: bool,
    log_params: dict,
    context: ExecutionContext,
) -> list[ProposedChange]:
    existing_tag_map = {tag["Key"]: tag["Value"] for tag in existing_tags}
    template_tag_map = {tag["Key"]: tag["Value"] for tag in template_tags}
    tags_to_apply = [
        tag for tag in template_tags if tag["Value"] != existing_tag_map.get(tag["Key"])
    ]
    tasks = []
    response = []

    if tags_to_remove := [
        tag["Key"] for tag in existing_tags if not template_tag_map.get(tag["Key"])
    ]:
        log_str = "Stale tags discovered."
        response.append(
            ProposedChange(
                change_type=ProposedChangeType.DETACH,
                attribute="tags",
                change_summary={"TagKeys": tags_to_remove},
            )
        )
        if not read_only:
            log_str = f"{log_str} Removing tags..."
            tasks.append(
                aio_wrapper(
                    iam_client.untag_policy,
                    PolicyArn=policy_arn,
                    TagKeys=tags_to_remove,
                )
            )
        log.info(log_str, tags=tags_to_remove, **log_params)

    if tags_to_apply:
        log_str = "New tags discovered in AWS."
        for tag in tags_to_apply:
            response.append(
                ProposedChange(
                    change_type=ProposedChangeType.ATTACH,
                    attribute="tags",
                    new_value=tag,
                )
            )
        if context.execute:
            log_str = f"{log_str} Adding tags..."
            tasks.append(
                aio_wrapper(
                    iam_client.tag_policy, PolicyArn=policy_arn, Tags=tags_to_apply
                )
            )
        log.info(log_str, tags=tags_to_apply, **log_params)

    if tasks:
        await asyncio.gather(*tasks)

    return response