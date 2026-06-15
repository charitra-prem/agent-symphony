"""Tiny Linear GraphQL client. Just enough to fetch an issue and post a comment."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"

_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    priority
    priorityLabel
    state { name type }
    labels { nodes { name } }
    team { id key name }
    project { id name }
    creator { name email }
    url
    createdAt
    updatedAt
  }
}
"""

_COMMENT_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
    comment { id url }
  }
}
"""

_COMMENT_UPDATE_MUTATION = """
mutation CommentUpdate($id: String!, $body: String!) {
  commentUpdate(id: $id, input: {body: $body}) {
    success
    comment { id url }
  }
}
"""

_REACTION_MUTATION = """
mutation ReactionCreate($commentId: String, $issueId: String, $emoji: String!) {
  reactionCreate(input: {commentId: $commentId, issueId: $issueId, emoji: $emoji}) {
    success
  }
}
"""


class LinearClient:
    def __init__(self, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("LINEAR_API_KEY not set")
        self._client = client or httpx.AsyncClient(
            base_url=LINEAR_API,
            headers={"Authorization": self.api_key, "Content-Type": "application/json"},
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post("", json={"query": query, "variables": variables})
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"linear graphql errors: {data['errors']}")
        return data.get("data", {})

    async def get_issue(self, issue_id: str) -> dict[str, Any]:
        d = await self._gql(_ISSUE_QUERY, {"id": issue_id})
        return d.get("issue") or {}

    async def post_comment(self, issue_id: str, body: str) -> dict[str, Any]:
        d = await self._gql(_COMMENT_MUTATION, {"issueId": issue_id, "body": body})
        return d.get("commentCreate") or {}

    async def edit_comment(self, comment_id: str, body: str) -> dict[str, Any]:
        d = await self._gql(_COMMENT_UPDATE_MUTATION, {"id": comment_id, "body": body})
        return d.get("commentUpdate") or {}

    async def react_to_issue(self, issue_id: str, emoji: str) -> dict[str, Any]:
        d = await self._gql(_REACTION_MUTATION, {"issueId": issue_id, "commentId": None, "emoji": emoji})
        return d.get("reactionCreate") or {}
