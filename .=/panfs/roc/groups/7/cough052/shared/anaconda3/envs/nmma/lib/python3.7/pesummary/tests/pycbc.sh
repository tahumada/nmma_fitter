# Licensed under an MIT style license -- see LICENSE.md

for ifo in H-H1 L-L1 V-V1; do
    file=${ifo}_LOSC_CLN_4_V1-1187007040-2048.gwf
    test -f ${file} && continue
    curl -O --silent https://dcc.ligo.org/public/0146/P1700349/001/${file}
done

curl -O https://pycbc.org/pycbc/latest/html/_downloads/7e72d7fbb3a44c0a0ee0ca04753ef47c/single.ini
sed -i '/no-save-data/d' ./single.ini
sed -i '/nlive = 500/a \dlogz = 1000' ./single.ini
pycbc_inference --config-file single.ini --output-file ./pycbc.hdf5
summarypages --webdir ./outdir/webpage --samples ./pycbc.hdf5 --gw --path_to_samples samples