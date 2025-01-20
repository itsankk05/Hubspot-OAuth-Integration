import datetime
import json
import secrets
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import asyncio
import base64
import hashlib

import requests
from integrations.integration_item import IntegrationItem

from redis_client import add_key_value_redis, get_value_redis, delete_key_redis


CLIENT_ID = "c1addc2a-84a6-4c5b-89ee-5b72c1b8e272"
CLIENT_SECRET = "74efccb3-1ae5-4aed-b249-a8d9c80d45aa"
REDIRECT_URI = "http://localhost:8000/integrations/hubspot/oauth2callback"
authorization_url = f"https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope=oauth%20crm.objects.companies.read"


encoded_client_id_secret = base64.b64encode(
    f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
).decode()


async def authorize_hubspot(user_id, org_id):
    state_data = {
        "state": secrets.token_urlsafe(32),
        "user_id": user_id,
        "org_id": org_id,
    }
    encoded_state = base64.urlsafe_b64encode(
        json.dumps(state_data).encode("utf-8")
    ).decode("utf-8")
    code_verifier = secrets.token_urlsafe(32)
    auth_url = f"{authorization_url}&state={encoded_state}"
    await asyncio.gather(
        add_key_value_redis(
            f"hubspot_state:{org_id}:{user_id}", json.dumps(state_data), expire=600
        ),
        add_key_value_redis(
            f"hubspot_verifier:{org_id}:{user_id}", code_verifier, expire=600
        ),
    )

    return auth_url


async def oauth2callback_hubspot(request: Request):
    # TODO
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400, detail=request.query_params.get("error_description")
        )
    code = request.query_params.get("code")
    encoded_state = request.query_params.get("state")
    state_data = json.loads(base64.urlsafe_b64decode(encoded_state).decode("utf-8"))

    original_state = state_data.get("state")
    user_id = state_data.get("user_id")
    org_id = state_data.get("org_id")

    saved_state, code_verifier = await asyncio.gather(
        get_value_redis(f"hubspot_state:{org_id}:{user_id}"),
        get_value_redis(f"hubspot_verifier:{org_id}:{user_id}"),
    )

    if not saved_state or original_state != json.loads(saved_state).get("state"):
        raise HTTPException(status_code=400, detail="State does not match.")

    async with httpx.AsyncClient() as client:
        response, _, _ = await asyncio.gather(
            client.post(
                "https://api.hubapi.com/oauth/v1/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ),
            delete_key_redis(f"hubspot_state:{org_id}:{user_id}"),
            delete_key_redis(f"hubspot_verifier:{org_id}:{user_id}"),
        )
        await add_key_value_redis(
            f"hubspot_credentials:{org_id}:{user_id}",
            json.dumps(response.json()),
            expire=600,
        ),

        close_window_script = """
            <html>
                <script>
                    window.close();
                </script>
            </html>
            """

        return HTMLResponse(content=close_window_script)


async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f"hubspot_credentials:{org_id}:{user_id}")
    if not credentials:
        raise HTTPException(status_code=400, detail="Credentials not found.")
    credentials = json.loads(credentials)
    await delete_key_redis(f"hubspot_credentials:{org_id}:{user_id}")

    return credentials


async def create_integration_item_metadata_object(
    response_json: dict, item_type: str, parent_id=None, parent_name=None
) -> IntegrationItem:
    """Create an integration item from HubSpot response"""
    try:
        item_id = response_json.get("id")
        properties = response_json.get("properties", {})

        parent_id = f"{parent_id}_Base" if parent_id else None

        return IntegrationItem(
            id=f"{item_id}_{item_type}",
            name=properties.get("name"),
            domain=properties.get("domain"),
            type=item_type,
            parent_id=parent_id,
            parent_path_or_name=parent_name,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error creating integration item: {str(e)}"
        )


def fetch_items(
    access_token: str, url: str, aggregated_response: list, limit=None
) -> dict:
    """Fetching the list of Companies"""
    params = {"limit": limit} if limit is not None else {}
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        results = response.json().get("results", {})
        limit = response.json().get("limit", None)

        for item in results:
            aggregated_response.append(item)

        if limit is not None:
            fetch_items(access_token, url, aggregated_response, limit)
        else:
            return


async def get_items_hubspot(credentials) -> list[IntegrationItem]:
    print(credentials)
    try:
        credentials_dict = json.loads(credentials)
        url = "https://api.hubapi.com/crm/v3/objects/companies"
        list_of_responses = []

        # Fetch all items
        fetch_items(credentials_dict.get("access_token"), url, list_of_responses)

        # Create integration items
        list_of_integration_item_metadata = []
        for response in list_of_responses:
            integration_item = await create_integration_item_metadata_object(
                response, "hubspot_company"
            )
            list_of_integration_item_metadata.append(integration_item)
            print(list_of_integration_item_metadata)

        return list_of_integration_item_metadata

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error fetching HubSpot items: {str(e)}"
        )
