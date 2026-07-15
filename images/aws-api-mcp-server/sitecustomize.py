"""Non-blocking patch for awslabs aws-api-mcp-server, auto-loaded at interpreter
startup via PYTHONPATH=/opt/patch (Python imports `sitecustomize` before main).

Why: the server's `call_aws` tool is `async def`, but it runs the actual AWS
call (`interpret_command` / `execute_awscli_customization`, both synchronous
botocore/CLI work) directly on the asyncio event loop. A long call — e.g.
`secretsmanager list-secrets` paginating a whole account — therefore blocks the
loop and freezes the server's MCP stdio protocol until it returns. Upstream that
freezes the ToolHive vMCP: its capability aggregation probes every backend's
`initialize` under one shared deadline, so a backend stuck mid-call cancels the
whole `tools/list` and takes the vMCP down for every request.

Fix: offload the two ctx-free blocking calls to a worker thread via
`asyncio.to_thread`, keeping all FastMCP `Context` I/O on the main loop, so the
server keeps answering protocol messages while a long AWS call runs.

This copies the body of `call_aws_helper` (it must introduce an `await` at the
blocking call sites, which a wrapper alone cannot). It is guarded: if the
upstream body drifts (expected markers missing) or anything else fails, the
patch is skipped and the server runs unmodified rather than breaking startup —
re-verify this copy whenever the base image ARG is bumped.
"""
import sys


def _apply() -> None:
    import asyncio
    import inspect

    from awslabs.aws_api_mcp_server import server as S

    orig = getattr(S, "call_aws_helper", None)
    if orig is None:
        raise RuntimeError("call_aws_helper not found on server module")
    src = inspect.getsource(orig)
    for marker in (
        "return interpret_command(",
        "execute_awscli_customization(",
        "is_awscli_customization",
        "is_help_operation",
    ):
        if marker not in src:
            raise RuntimeError(f"unexpected call_aws_helper body (missing marker {marker!r})")

    async def call_aws_helper(cli_command, ctx, max_results=None, credentials=None, default_region=None):
        """Patched call_aws_helper: the blocking AWS execution runs off the event
        loop (asyncio.to_thread) so long calls don't freeze the MCP protocol.
        Body mirrors upstream; only the two terminal blocking calls are awaited."""
        try:
            ir = S.translate_cli_to_ir(cli_command)
            ir_validation = S.validate(ir)
            if not ir.command or ir_validation.validation_failed:
                error_message = f'Error while validating the command: {ir_validation.model_dump_json()}'
                await ctx.error(error_message)
                raise S.CommandValidationError(error_message)
        except S.AwsApiMcpError as e:
            await ctx.error(e.as_failure().reason)
            raise
        except Exception as e:
            error_message = f'Error while validating the command: {str(e)}'
            await ctx.error(error_message)
            raise S.AwsApiMcpError(error_message)

        S.logger.info(
            'Attempting to execute AWS CLI command: aws {} {} *parameters redacted*',
            ir.command.service_name,
            ir.command.operation_cli_name,
        )

        try:
            if S.READ_OPERATIONS_INDEX is not None:
                policy_decision = S.check_security_policy(ir, S.READ_OPERATIONS_INDEX, ctx)
                if policy_decision == S.PolicyDecision.DENY:
                    error_message = 'Execution of this operation is denied by security policy.'
                    await ctx.error(error_message)
                    raise S.AwsApiMcpError(error_message)
                elif policy_decision == S.PolicyDecision.ELICIT:
                    await S.request_consent(cli_command, ctx)
            else:
                if S.READ_OPERATIONS_ONLY_MODE:
                    error_message = (
                        'Execution of this operation is not allowed because read only mode is enabled. '
                        f'It can be disabled by setting the {S.READ_ONLY_KEY} environment variable to False.'
                    )
                    await ctx.error(error_message)
                    raise S.AwsApiMcpError(error_message)
                elif S.REQUIRE_MUTATION_CONSENT:
                    await S.request_consent(cli_command, ctx)

            if ir.command and ir.command.is_help_operation:
                return await S.get_help_document(cli_command, ctx)

            if ir.command and ir.command.is_awscli_customization:
                # off-loop: blocking CLI customization (e.g. s3 sync)
                return await asyncio.to_thread(
                    S.execute_awscli_customization,
                    cli_command,
                    ir.command,
                    credentials=credentials,
                    default_region_override=default_region,
                )

            # off-loop: blocking botocore execution + pagination (the hot path)
            return await asyncio.to_thread(
                S.interpret_command,
                cli_command=cli_command,
                max_results=max_results,
                credentials=credentials,
                default_region_override=default_region,
            )
        except S.NoCredentialsError:
            error_message = (
                'Error while executing the command: No AWS credentials found. '
                "Please configure your AWS credentials using 'aws configure' "
                'or set appropriate environment variables.'
            )
            await ctx.error(error_message)
            raise S.AwsApiMcpError(error_message)
        except S.AwsApiMcpError as e:
            await ctx.error(e.as_failure().reason)
            raise
        except Exception as e:
            error_message = f'Error while executing the command: {str(e)}'
            await ctx.error(error_message)
            raise S.AwsApiMcpError(error_message)

    S.call_aws_helper = call_aws_helper
    print(
        "[aws-api-mcp-nonblocking] patched call_aws_helper to run blocking AWS calls off the event loop",
        file=sys.stderr,
    )


try:
    _apply()
except Exception as exc:  # never break interpreter startup
    print(f"[aws-api-mcp-nonblocking] patch skipped ({exc!r}); server runs unpatched", file=sys.stderr)
