// TxLINE devnet onboarding: on-chain subscribe -> guest JWT -> sign txSig::jwt -> activate API token.
// Flow verified against https://txline.txodds.com/documentation/quickstart,
// https://txline-docs.txodds.com/documentation/worldcup, and the reference example at
// https://github.com/txodds/tx-on-chain/blob/main/examples/devnet/scripts/subscription_free_tier.ts

import * as fs from "fs";
import * as path from "path";
import * as anchor from "@coral-xyz/anchor";
import { Connection, Keypair, PublicKey, SystemProgram } from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_2022_PROGRAM_ID,
  createAssociatedTokenAccountInstruction,
  getAccount,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import axios from "axios";
import bs58 from "bs58";
import nacl from "tweetnacl";
import dotenv from "dotenv";
import txoracleIdl from "./idl.json";

dotenv.config();

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required env var: ${name}`);
  return value;
}

async function main() {
  const rpcUrl = requireEnv("SOLANA_RPC_URL");
  if (!rpcUrl.includes("devnet")) {
    throw new Error(`Refusing to run: SOLANA_RPC_URL must be a devnet endpoint, got ${rpcUrl}`);
  }

  const programIdEnv = requireEnv("TXLINE_PROGRAM_ID");
  const apiOrigin = requireEnv("TXLINE_API_BASE").replace(/\/$/, "");
  const apiBaseUrl = `${apiOrigin}/api`;
  const tokenMint = new PublicKey(requireEnv("TXLINE_TOKEN_MINT"));

  const serviceLevelId = Number(process.env.SERVICE_LEVEL_ID ?? "1");
  if (serviceLevelId !== 1) {
    throw new Error("Refusing to run: this onboarding script is locked to SERVICE_LEVEL_ID=1 (free World Cup tier).");
  }

  const weeks = Number(process.env.DURATION_WEEKS ?? "4");
  if (weeks < 4 || weeks % 4 !== 0) {
    throw new Error(`Invalid DURATION_WEEKS: ${weeks}. The program requires a multiple of 4.`);
  }

  const leagues = (process.env.LEAGUES ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map(Number);

  const privateKeyBase58 = requireEnv("WALLET_PRIVATE_KEY");
  const userKeypair = Keypair.fromSecretKey(bs58.decode(privateKeyBase58));
  // Never log privateKeyBase58 or userKeypair.secretKey beyond this point.

  console.log("Wallet public key:", userKeypair.publicKey.toBase58());

  const connection = new Connection(rpcUrl, "confirmed");
  const wallet = new anchor.Wallet(userKeypair);
  const provider = new anchor.AnchorProvider(connection, wallet, { commitment: "confirmed" });
  anchor.setProvider(provider);

  const program = new anchor.Program(txoracleIdl as anchor.Idl, provider);
  const expectedProgramId = new PublicKey(programIdEnv);
  if (!program.programId.equals(expectedProgramId)) {
    throw new Error(
      `Loaded IDL program ${program.programId.toBase58()} does not match TXLINE_PROGRAM_ID ${expectedProgramId.toBase58()}`
    );
  }

  const balanceLamports = await connection.getBalance(userKeypair.publicKey);
  if (balanceLamports < 0.01 * anchor.web3.LAMPORTS_PER_SOL) {
    console.log(`Low devnet SOL balance (${balanceLamports} lamports). Requesting airdrop...`);
    try {
      const sig = await connection.requestAirdrop(userKeypair.publicKey, anchor.web3.LAMPORTS_PER_SOL);
      await connection.confirmTransaction(sig, "confirmed");
      console.log("Airdrop confirmed.");
    } catch (err) {
      console.warn(
        "Airdrop failed (devnet faucet is often rate-limited). " +
          "Fund the wallet manually at https://faucet.solana.com before retrying.",
        err instanceof Error ? err.message : err
      );
    }
  }

  const userTokenAccountAddress = getAssociatedTokenAddressSync(
    tokenMint,
    userKeypair.publicKey,
    false,
    TOKEN_2022_PROGRAM_ID
  );

  const [pricingMatrixPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("pricing_matrix")],
    program.programId
  );
  const [tokenTreasuryPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("token_treasury_v2")],
    program.programId
  );
  const tokenTreasuryVault = getAssociatedTokenAddressSync(
    tokenMint,
    tokenTreasuryPda,
    true,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );

  const existingAccountInfo = await connection.getAccountInfo(userTokenAccountAddress);
  if (!existingAccountInfo) {
    console.log("Creating TXL (Token-2022) associated token account for wallet...");
    const createAtaTx = new anchor.web3.Transaction().add(
      createAssociatedTokenAccountInstruction(
        userKeypair.publicKey,
        userTokenAccountAddress,
        userKeypair.publicKey,
        tokenMint,
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID
      )
    );
    await anchor.web3.sendAndConfirmTransaction(connection, createAtaTx, [userKeypair], {
      commitment: "confirmed",
    });
  }

  let attempts = 0;
  while (attempts < 5) {
    try {
      await getAccount(connection, userTokenAccountAddress, "confirmed", TOKEN_2022_PROGRAM_ID);
      break;
    } catch (err) {
      attempts++;
      if (attempts >= 5) throw new Error("RPC failed to sync the new token account after 5 attempts.");
      console.log(`Token account not visible yet, retrying (${attempts}/5)...`);
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }

  console.log(`Subscribing on-chain: service level ${serviceLevelId}, ${weeks} weeks, leagues=[${leagues}]`);
  const txSig = await program.methods
    .subscribe(serviceLevelId, weeks)
    .accounts({
      user: userKeypair.publicKey,
      pricingMatrix: pricingMatrixPda,
      tokenMint: tokenMint,
      userTokenAccount: userTokenAccountAddress,
      tokenTreasuryVault: tokenTreasuryVault,
      tokenTreasuryPda: tokenTreasuryPda,
      tokenProgram: TOKEN_2022_PROGRAM_ID,
      associatedTokenProgram: ASSOCIATED_TOKEN_PROGRAM_ID,
      systemProgram: SystemProgram.programId,
    })
    .rpc();
  console.log("On-chain subscribe confirmed. txSig:", txSig);

  console.log("Requesting guest JWT...");
  const guestResponse = await axios.post(`${apiOrigin}/auth/guest/start`);
  const jwt: string = guestResponse.data.token;

  const messageString = `${txSig}:${leagues.join(",")}:${jwt}`;
  const message = new TextEncoder().encode(messageString);
  const signatureBytes = nacl.sign.detached(message, userKeypair.secretKey);
  const walletSignature = Buffer.from(signatureBytes).toString("base64");

  console.log("Activating API token...");
  const activationResponse = await axios.post(
    `${apiBaseUrl}/token/activate`,
    { txSig, walletSignature, leagues },
    { headers: { Authorization: `Bearer ${jwt}` } }
  );
  const apiToken: string = activationResponse.data.token || activationResponse.data;

  const credentials = {
    walletPublicKey: userKeypair.publicKey.toBase58(),
    txSig,
    jwt,
    apiToken,
    serviceLevelId,
    weeks,
    leagues,
    apiBaseUrl,
    activatedAt: new Date().toISOString(),
  };

  const outPath = path.join(__dirname, "credentials.json");
  fs.writeFileSync(outPath, JSON.stringify(credentials, null, 2));
  console.log("Credentials written to", outPath);
}

main().then(
  () => process.exit(0),
  (err) => {
    console.error("Onboarding failed:", err instanceof Error ? err.message : err);
    process.exit(1);
  }
);
