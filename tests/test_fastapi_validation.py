from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import Engine, inspect
from sqlmodel import Field, Session, SQLModel, select


@pytest.mark.parametrize("nested", [False, True])
def test_fastapi_validates_table_bodies_before_persistence(
    database_engine: Engine, nested: bool
) -> None:
    calls: list[str] = []
    handled: list[int] = []

    class Order(SQLModel, table=True):
        id: int | None = Field(default=None, primary_key=True)
        quantity: int = Field(gt=0)

        @field_validator("quantity")
        @classmethod
        def record_field(cls, value: int) -> int:
            calls.append("field")
            return value

        def model_post_init(self, context: Any) -> None:
            assert inspect(self).object is self
            calls.append("post")

        @model_validator(mode="after")
        def check_quantity(self) -> "Order":
            calls.append("after")
            if self.quantity > 10:
                raise ValueError("Quantity exceeds limit")
            return self

    class Envelope(BaseModel):
        order: Order

    class OrderPublic(SQLModel):
        id: int
        quantity: int

    SQLModel.metadata.create_all(database_engine)
    app = FastAPI()

    def persist(order: Order) -> Order:
        assert calls == ["field", "post", "after"]
        handled.append(order.quantity)
        with Session(database_engine) as session:
            session.add(order)
            session.commit()
            session.refresh(order)
        return order

    if nested:

        @app.post("/orders", response_model=OrderPublic)
        def create_nested(payload: Envelope) -> Order:
            return persist(payload.order)

    else:

        @app.post("/orders", response_model=OrderPublic)
        def create(order: Order) -> Order:
            return persist(order)

    with TestClient(app) as client:
        data = {"quantity": "2"}
        response = client.post("/orders", json={"order": data} if nested else data)
        assert response.status_code == 200, response.text
        assert response.json() == {"id": 1, "quantity": 2}

        for invalid in ({}, {"quantity": -1}, {"quantity": 11}):
            calls.clear()
            response = client.post(
                "/orders", json={"order": invalid} if nested else invalid
            )
            assert response.status_code == 422, response.text
            assert response.json()["detail"][0]["loc"][0] == "body"
        assert handled == [2]

        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["components"]["schemas"]["Order"]["required"] == [
            "quantity"
        ]

    with Session(database_engine) as session:
        orders = session.exec(select(Order)).all()
        assert len(orders) == 1
        assert orders[0].quantity == 2
