# Joined-table inheritance

This fork supports a mapped child with its own explicitly declared table name.
SQLAlchemy maps the parent and child tables using a foreign key between them.
Inherited fields stay in the parent table; fields declared on the child belong
to the child table.

```python
from sqlmodel import Field, Session, SQLModel, create_engine, select


class Resource(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    kind: str = Field(polymorphic=True, polymorphic_identity="resource")


class Webhook(Resource, table=True):
    __tablename__ = "webhook"

    resource_id: int | None = Field(
        default=None, primary_key=True, foreign_key="resource.id"
    )
    path: str

engine = create_engine("sqlite://")
SQLModel.metadata.create_all(engine)

with Session(engine) as session:
    session.add(Webhook(name="Orders", path="/orders"))
    session.commit()

with Session(engine) as session:
    resource = session.exec(select(Resource)).one()
    assert isinstance(resource, Webhook)
    assert resource.id == resource.resource_id
    assert resource.path == "/orders"
```

There is one resource row and one webhook row. `Session.add`, updates, and
`Session.delete` use SQLAlchemy's normal unit of work for both tables. No custom
SQLModel persistence layer is involved.

The child foreign key can instead redeclare `id` with a foreign key to the
parent's primary key. SQLAlchemy handles the combined identity in either case.

## Declare the discriminator on a field

`Field(polymorphic=True)` selects the column SQLAlchemy uses to choose a class
when loading rows through the parent model. The column is created only on the
root table, so schema migration tools can inspect it normally.

`polymorphic_identity` supplies the value identifying a class. Without an explicit
identity, the class name in lowercase is used (`Webhook` becomes `"webhook"`).
Each mapped subclass receives its own identity as a Pydantic default, including
when created through `model_validate`, JSON validation, `TypeAdapter`, or
`model_construct`. The parent's field definition is not changed.

To keep an identity stable across Python class renames, declare it on the
inherited discriminator field in the child:

```python
kind: str = Field(polymorphic_identity="webhook")
```

This changes the child's mapper identity and Pydantic default, without creating
another `kind` column in the child table. The field remains a normal typed input;
its identity is a default, not a constraint restricting explicitly supplied values.
Changing an identity already stored in the database requires a data migration;
renaming the Python class does not rewrite existing rows.

Explicit `__mapper_args__` remain available for native SQLAlchemy customization.
For field-declared polymorphism, an explicit `polymorphic_identity` in that
dictionary overrides both the field option and the inferred class name. Models
configured solely through `__mapper_args__` retain their existing Pydantic defaults.

`polymorphic` is separate from Pydantic's existing `Field(discriminator=...)`,
which selects members of a union during validation rather than ORM loading.

An explicit child `__tablename__` opts into mapping. Ordinary subclasses without
their own table name remain unmapped, preserving response-model cloning.
`model_config = {"table": False}` also disables mapping. This cut does not add
single-table or concrete-table inheritance APIs.

Inherited field requirements, defaults, constraints, and relationship inputs are
preserved during validation without redeclaring fields or modifying the parent's
definitions. Explicitly redeclaring a mapped field, such as `id` or `kind`, still produces
Pydantic's normal field-shadowing warning.

Inheritance mapping, joins, discriminator behavior, and loading strategies remain
SQLAlchemy responsibilities. See
[SQLAlchemy's inheritance documentation](https://docs.sqlalchemy.org/en/20/orm/inheritance.html#joined-table-inheritance).
