import os
import inspect
import argparse
import typing
from argparse import Namespace

def compile_current_function_args(**kwargs):
    """Compiles the arguments of the current function into a Namespace object."""
    frame = inspect.currentframe().f_back
    if frame is None:
        raise ValueError("No frame found")
    arg_names, _, _, locals = inspect.getargvalues(frame)
    # arg_names is the list of argument names of the function
    # locals is the dictionary of local variables in the function (superset of arg_names, including locally-defined values)
    args = {arg: locals[arg] for arg in arg_names}
    args.update(kwargs)
    return Namespace(**args)

def get_env_var(*varnames):
    """
    Get one or more environment variables.

    Raises:
        ValueError: If no variable names are specified

    Returns:
        any or list: Returns a single variable if only one name was specfied, else a tuple.
    """
    if len(varnames) == 0:
        raise ValueError("At least one environment variable name must be provided.")
    v = tuple(os.environ.get(name) for name in varnames)
    return v[0] if len(varnames) == 1 else v


def _str_to_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in ('false', 'f', '0', 'no', 'n'):
        return False
    elif value.lower() in ('true', 't', '1', 'yes', 'y'):
        return True
    raise ValueError(f'{value} is not a valid boolean value')


def load_func_argparser(func, ignore=None):
    """
    Instantiates an ArgumentParser with arguments for each argument in func, except those in ignore.

    Args:
        func (callable): Function with which to parse args.
        ignore (iterable[str], optional): Function parameter names to ignore. Defaults to None.

    Returns:
        argparse.ArgumentParser: Arg parser
    """
    parser = argparse.ArgumentParser()
    signature = inspect.signature(func)
    _get_or_none = lambda x: None if x is inspect._empty else x

    for name, param in signature.parameters.items():
        if ignore is not None and name in ignore:
            continue

        dtype = _get_or_none(param.annotation)
        default = _get_or_none(param.default)
        kwargs = {
            "type": dtype,
            "default": default,
        }

        if param.default is inspect._empty:
            # Required argument
            kwargs["required"] = True
            kwargs["help"] = f"(*{dtype.__name__}, required)"
        else:
            kwargs["help"] = f"({dtype.__name__}, Default: {default})" if default is not None else None

        if typing.get_origin(dtype) == typing.Literal:
            # Correct way to check if a type is a Literal
            # https://docs.python.org/3.9/library/typing.html#typing.get_origin
            kwargs["choices"] = list(dtype.__args__)
            del kwargs["type"]  # can't have both type and choices

        if dtype == bool:
            # kwargs["action"] = argparse.BooleanOptionalAction
            # kwargs["action"] = "store_true" if default is False else "store_false"
            # del kwargs["type"]  # can't have both type and store_true action

            kwargs["type"] = _str_to_bool
            kwargs["nargs"] = "?"  # so just `--flag` is equivalent to True if default is False
            kwargs["const"] = True if default is False else False
            kwargs["default"] = default
            if default is False:
                kwargs["help"] += f" Use `--{name}` to set to True. (Alternatively `--{name} True/False`)"
            kwargs["help"] += f" Use `--{name} True/False` to change."

        parser.add_argument(f"--{name}", **kwargs)
    
    return parser