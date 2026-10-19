// SPDX-License-Identifier: MIT
pragma solidity 0.8.31;

import {Script, console} from "forge-std/Script.sol";
import {TCZK} from "../src/TCZK.sol";

interface IEscrowStore {
    function getTokenStore(address operator) external view returns (address);
}

/// Deploys tCZK and sets it up for the demo in one run (SPEC §3.3–§3.5):
/// roles, allowlist (treasury, client wallets from fixtures/companies.json, escrow collector,
/// the operator's token store), ROUTER for everything that moves money, demo mint to Alfa.
/// The broadcaster becomes admin, issuer, compliance and enforcer (the bank's admin key).
/// Env (all optional): OPERATOR_ADDR, RELAYER_ADDR (default: broadcaster), ACCOUNT_REF_SALT, MINT_ALFA.
/// Commands: contracts/README.md.
contract Deploy is Script {
    address constant ESCROW = 0xf96815976523E00e65Be8f34cA5e64b4f41EB19c;
    address constant COLLECTOR = 0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88;
    address constant REFUND_COLLECTOR = 0x7a03443724d14798c4AB4622F1DAAcA761Fea486;

    function run() external returns (TCZK token) {
        address operator = vm.envOr("OPERATOR_ADDR", address(0x0469ceC178789072bc61435835D0BB38214D4e53));
        string memory salt = vm.envOr("ACCOUNT_REF_SALT", string("collabse-demo"));
        uint256 mintAlfa = vm.envOr("MINT_ALFA", uint256(100_000)); // 1000.00 Kč
        address store = IEscrowStore(ESCROW).getTokenStore(operator);
        string memory json = vm.readFile(string.concat(vm.projectRoot(), "/../fixtures/companies.json"));

        vm.startBroadcast();
        (, address admin,) = vm.readCallers();
        address relayer = vm.envOr("RELAYER_ADDR", admin);
        token = new TCZK(admin);
        token.grantRole(token.ISSUER_ROLE(), admin);
        token.grantRole(token.COMPLIANCE_ROLE(), admin);
        token.grantRole(token.ENFORCER_ROLE(), admin);
        // Strict mode: everyone who moves tCZK on the bank's behalf (§3.4)
        token.grantRole(token.ROUTER_ROLE(), relayer);
        token.grantRole(token.ROUTER_ROLE(), COLLECTOR);
        token.grantRole(token.ROUTER_ROLE(), store);
        token.grantRole(token.ROUTER_ROLE(), REFUND_COLLECTOR);
        // Allowlist (§3.3): treasury, clients, escrow infrastructure — before the first authorize
        token.allow(admin, keccak256(abi.encodePacked(salt, "bank-treasury")));
        address alfa;
        for (uint256 i = 0; vm.keyExistsJson(json, string.concat(".companies[", vm.toString(i), "]")); i++) {
            string memory c = string.concat(".companies[", vm.toString(i), "]");
            address wallet = vm.parseJsonAddress(json, string.concat(c, ".wallet_address"));
            string memory account = vm.parseJsonString(json, string.concat(c, ".account_number"));
            token.allow(wallet, keccak256(abi.encodePacked(salt, account)));
            if (keccak256(bytes(vm.parseJsonString(json, string.concat(c, ".id")))) == keccak256("alfa")) alfa = wallet;
        }
        token.allow(COLLECTOR, bytes32(0));
        token.allow(store, bytes32(0));
        if (mintAlfa > 0) token.mint(alfa, mintAlfa, "demo-deposit-alfa");
        vm.stopBroadcast();

        console.log("TCZK=%s", address(token));
        console.log("TCZK_NAME=\"%s\"", token.name());
        console.log("admin %s relayer %s", admin, relayer);
        console.log("operator %s token store %s", operator, store);
        console.log("alfa %s balance %s", alfa, token.balanceOf(alfa));
    }
}
