"""One-time on-chain token approvals for an EOA (MetaMask-style) wallet to
trade on Polymarket's CLOB.

Only needed for signature_type=0 (plain EOA) wallets paying their own gas.
Polymarket's email/Magic-link wallets (signature_type=1) and Gnosis Safe /
proxy wallets (signature_type=2) get these allowances set up gaslessly by
Polymarket's relayer and do not need this script.

Approves, for each of USDC.e and the CTF (conditional token) contract, the
three Polymarket exchange contracts that need to move your funds to fill
orders: the CTF Exchange, the Negative-Risk CTF Exchange, and the
Negative-Risk Adapter. Requires POL (native gas token on Polygon) in the
wallet to pay for these transactions.

Reference: https://gist.github.com/poly-rodr/44313920481de58d5a3f6d1f8226bd5e
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

POLYGON_RPC_URL = "https://polygon-rpc.com"

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

SPENDERS = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Negative-Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Negative-Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

ERC1155_SET_APPROVAL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "operator", "type": "address"}, {"internalType": "bool", "name": "approved", "type": "bool"}],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def run_allowance_setup(private_key: str, chain_id: int = 137, rpc_url: str = POLYGON_RPC_URL) -> None:
    from web3 import Web3
    from web3.constants import MAX_INT

    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
    except ImportError:  # older web3.py versions
        from web3.middleware import geth_poa_middleware as _POAMiddleware  # type: ignore

    web3 = Web3(Web3.HTTPProvider(rpc_url))
    try:
        web3.middleware_onion.inject(_POAMiddleware, layer=0)
    except ValueError:
        pass  # already injected

    account = web3.eth.account.from_key(private_key)
    pub_key = account.address
    logger.info("Setting allowances for wallet %s", pub_key)

    usdc = web3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
    ctf = web3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_SET_APPROVAL_ABI)

    nonce = web3.eth.get_transaction_count(pub_key)
    max_amount = int(MAX_INT, 0)

    for name, spender in SPENDERS.items():
        spender = Web3.to_checksum_address(spender)

        logger.info("Approving USDC.e for %s (%s)...", name, spender)
        tx = usdc.functions.approve(spender, max_amount).build_transaction(
            {"chainId": chain_id, "from": pub_key, "nonce": nonce}
        )
        signed = web3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("  tx: %s", tx_hash.hex())
        nonce += 1

        logger.info("Approving conditional tokens for %s (%s)...", name, spender)
        tx = ctf.functions.setApprovalForAll(spender, True).build_transaction(
            {"chainId": chain_id, "from": pub_key, "nonce": nonce}
        )
        signed = web3.eth.account.sign_transaction(tx, private_key=private_key)
        tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("  tx: %s", tx_hash.hex())
        nonce += 1

    logger.info(
        "All approval transactions submitted. They may take a few seconds to confirm on Polygon; "
        "check https://polygonscan.com/address/%s once done.", pub_key
    )


if __name__ == "__main__":
    import os
    import sys

    logging.basicConfig(level=logging.INFO)
    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("Set POLYMARKET_PRIVATE_KEY in your environment first.", file=sys.stderr)
        raise SystemExit(1)
    run_allowance_setup(key)
