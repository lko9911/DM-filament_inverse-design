function po = printingOrder(Ti,Tdef,Seg)


while true
    loop = input('Manually define printing order? (y/n) : ','s'); % 0 to continue 1 to break loop
    if loop == 'y'
        po = input('Write printing order in 1xN array :  ');
        po = po';
        break
    else
        mn = unique(Seg(:,1));
        ml = zeros(size(mn));
        
        for i = 1:length(mn)
            c = find(mn(i) == Seg(:,1));
            ml(i) = sum(Seg(c,3)-Seg(c,2));
        end
        [~,ii] = max(ml);
        po(1) = mn(ii);
        
        for i=1:size(Ti,1)
            for j = 1:size(Tdef,1)
                if Ti(i,1) == Tdef{j,1}    % Compare Tin and Tdef to find M information of specific T
                    Minfo = Tdef{j,2};
                end
            end         
            
            
            poCheck = Minfo(:,1) == po;
            if sum(poCheck) == 0
                for mm = 1:size(Minfo,1)
                    puCheck = Seg(1:i,1) == Minfo(mm,1);
                    if sum(puCheck) == 0        % check if the material is not used in previous segment
                        po = [po; Minfo(mm,1)];
                    end
                end
            end
        end
        
        while length(po) < length(unique(Seg(:,1)))
            for i=1:size(Seg,1)
                if Seg(i,1) ~= po
                    po = [po; Seg(i,1)];
                end
            end
        end
        break
    end
end

end